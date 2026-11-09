"""图片库：按兴趣搜图 / 入库 / 配文动态 / 生命周期清理。

设计约束（防内存/磁盘爆炸）：
- 下载走流式 + 单文件上限（``SE_MEDIA_DOWNLOAD_MAX_BYTES``，默认 12MB），不把整张原图读进内存；
- 落盘统一压缩成 900px webp（复用 :func:`app.v3.media.download_image`）；
- 总上限 ``SE_MEDIA_MAX_BYTES``（默认 200MB）/ ``SE_MEDIA_MAX_FILES``（默认 800 张）；
- 清理顺序：① 过期且没被任何动态引用的图 ② DB 里文件已丢的孤儿记录 ③ 超配额时按最旧未引用图淘汰；
- **被 persona_posts.media 引用的图永不删除**（宁可超配额也不弄坏线上动态）；
- 后台每小时跑一次 janitor（见 main.py）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

import httpx

from app.logging_utils import get_logger
from app.v3 import media as media_mod

log = get_logger(__name__)

DEFAULT_MAX_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_FILES = 800
DEFAULT_TTL_DAYS = 45
DEFAULT_DOWNLOAD_MAX_BYTES = 12 * 1024 * 1024


def _num(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def limits() -> dict[str, int]:
    return {
        "max_bytes": _num("SE_MEDIA_MAX_BYTES", DEFAULT_MAX_BYTES, 10 * 1024 * 1024, 4 * 1024**3),
        "max_files": _num("SE_MEDIA_MAX_FILES", DEFAULT_MAX_FILES, 20, 20000),
        "ttl_days": _num("SE_MEDIA_TTL_DAYS", DEFAULT_TTL_DAYS, 1, 3650),
        "download_max_bytes": _num(
            "SE_MEDIA_DOWNLOAD_MAX_BYTES", DEFAULT_DOWNLOAD_MAX_BYTES, 256 * 1024, 64 * 1024 * 1024
        ),
    }


def media_dir() -> str:
    from app.config import get_settings  # noqa: PLC0415

    return os.path.join(str(get_settings().data_path), "media")


def _referenced_paths(db: Any) -> set[str]:
    """所有 persona_posts.media 里引用的 /media/xxx 路径。"""
    refs: set[str] = set()
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT media FROM persona_posts WHERE media<>'[]' AND media<>''"
            ).fetchall()
    except sqlite3.Error:
        return refs
    for row in rows:
        try:
            paths = json.loads(str(row["media"] or "[]"))
        except (TypeError, ValueError, KeyError):
            continue
        if isinstance(paths, list):
            for path in paths:
                text = str(path or "").strip()
                if text:
                    refs.add(text)
    return refs


def _scan_files(root: str) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    if not os.path.isdir(root):
        return files
    with os.scandir(root) as it:
        for entry in it:
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            files[f"/media/{entry.name}"] = {
                "path": entry.path,
                "name": entry.name,
                "size": int(stat.st_size),
                "mtime": float(stat.st_mtime),
            }
    return files


def stats(db: Any) -> dict[str, Any]:
    root = media_dir()
    files = _scan_files(root)
    referenced = _referenced_paths(db)
    total_bytes = sum(item["size"] for item in files.values())
    ref_files = [p for p in files if p in referenced]
    ref_bytes = sum(files[p]["size"] for p in ref_files)
    try:
        with db._lock:  # noqa: SLF001
            db_rows = int(db.conn.execute("SELECT COUNT(*) FROM media_library").fetchone()[0])
            oldest = db.conn.execute("SELECT MIN(fetched_at) FROM media_library").fetchone()[0]
    except sqlite3.Error:
        db_rows = 0
        oldest = ""
    return {
        "ok": True,
        "dir": root,
        "files": len(files),
        "bytes": total_bytes,
        "bytes_mb": round(total_bytes / 1024 / 1024, 2),
        "referenced_files": len(ref_files),
        "referenced_bytes_mb": round(ref_bytes / 1024 / 1024, 2),
        "unreferenced_files": len(files) - len(ref_files),
        "db_rows": db_rows,
        "oldest_fetched_at": str(oldest or ""),
        "limits": limits(),
    }


def prune(
    db: Any,
    *,
    ttl_days: int | None = None,
    max_bytes: int | None = None,
    max_files: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """清理图库：过期未引用 → 缺文件行 → 超配额未引用（最旧优先）。"""
    conf = limits()
    ttl = conf["ttl_days"] if ttl_days is None else max(1, int(ttl_days))
    cap_bytes = conf["max_bytes"] if max_bytes is None else max(1024, int(max_bytes))
    cap_files = conf["max_files"] if max_files is None else max(1, int(max_files))
    root = media_dir()
    files = _scan_files(root)
    referenced = _referenced_paths(db)
    now = time.time()
    doomed: list[tuple[str, dict[str, Any], str]] = []  # (path, fileinfo, reason)
    for path, info in files.items():
        if path in referenced:
            continue
        if now - info["mtime"] > ttl * 86400:
            doomed.append((path, info, "ttl"))
    keep = {p for p in files if p not in {d[0] for d in doomed}}
    total_bytes = sum(files[p]["size"] for p in keep)
    if len(keep) > cap_files or total_bytes > cap_bytes:
        for path in sorted(keep, key=lambda p: files[p]["mtime"]):
            if path in referenced:
                continue
            info = files[path]
            doomed.append((path, info, "quota"))
            total_bytes -= info["size"]
            keep.discard(path)
            if len(keep) <= cap_files and total_bytes <= cap_bytes:
                break

    deleted_files = 0
    deleted_bytes = 0
    deleted_rows = 0
    errors: list[str] = []
    for path, info, _reason in doomed:
        if dry_run:
            deleted_files += 1
            deleted_bytes += info["size"]
            continue
        try:
            os.remove(info["path"])
            deleted_files += 1
            deleted_bytes += info["size"]
        except OSError as exc:
            errors.append(f"{info['name']}: {exc}")
        try:
            with db._lock, db.conn:  # noqa: SLF001
                db.conn.execute("DELETE FROM media_library WHERE path=?", (path,))
                deleted_rows += 1
        except sqlite3.Error:
            pass

    # DB 行存在但文件已丢 → 删行
    try:
        with db._lock:  # noqa: SLF001
            rows = [str(r["path"] or "") for r in db.conn.execute("SELECT path FROM media_library").fetchall()]
        orphans = [p for p in rows if p and p not in files]
        if orphans and not dry_run:
            with db._lock, db.conn:  # noqa: SLF001
                for path in orphans:
                    db.conn.execute("DELETE FROM media_library WHERE path=?", (path,))
    except sqlite3.Error:
        orphans = []

    remaining = _scan_files(root)
    return {
        "ok": True,
        "dry_run": dry_run,
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
        "deleted_rows": deleted_rows,
        "removed_missing_rows": len(orphans),
        "remaining_files": len(remaining),
        "remaining_mb": round(sum(i["size"] for i in remaining.values()) / 1024 / 1024, 2),
        "referenced_kept": len(referenced & set(remaining)),
        "limits": {"max_files": cap_files, "max_mb": round(cap_bytes / 1024 / 1024, 2), "ttl_days": ttl},
        "errors": errors[:5],
    }


def _persona_row(db: Any, persona_id: str) -> dict[str, Any] | None:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
    return dict(row) if row is not None else None


async def search(
    db: Any, query: str, *, persona_id: str = "", limit: int = 8
) -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "issues": ["搜索词不能为空"]}
    limit = max(1, min(int(limit), 20))
    persona = _persona_row(db, persona_id) if persona_id else {}
    try:
        with db._lock:  # noqa: SLF001
            known = {
                str(r["external_id"])
                for r in db.conn.execute("SELECT external_id FROM media_library").fetchall()
            }
    except sqlite3.Error:
        known = set()
    items: list[Any] = []
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        for adapter in (media_mod.WikimediaAdapter(), media_mod.OpenverseAdapter()):
            try:
                found = await adapter.search(client, query, limit=limit * 2)
            except Exception as exc:  # noqa: BLE001
                log.debug("图库搜索失败 %s: %s", adapter.name, exc)
                continue
            items.extend(found)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if item.external_id in seen:
            continue
        seen.add(item.external_id)
        if not media_mod.is_life_safe(item, persona or {}):
            continue
        if not media_mod.relevant_to_query(item, query):
            continue
        out.append(
            {
                "external_id": item.external_id,
                "source": item.source,
                "title": item.title,
                "tags": list(item.tags or [])[:10],
                "license": item.license,
                "author": item.author,
                "page_url": item.page_url,
                "thumb_url": item.thumb_url or item.image_url,
                "downloaded": item.external_id in known,
            }
        )
        if len(out) >= limit:
            break
    return {"ok": True, "query": query, "persona_id": persona_id, "items": out}


async def fetch(
    db: Any,
    query: str,
    *,
    persona_id: str = "",
    limit: int = 2,
    with_caption: bool = False,
    llm: Any = None,
    external_id: str = "",
) -> dict[str, Any]:
    """搜索并下载入库；返回新增图片（可选：立即用其中一张发一条配文动态）。"""
    limit = max(1, min(int(limit), 6))
    status = prune(db)
    conf = limits()
    if status["remaining_files"] >= conf["max_files"] or status["remaining_mb"] * 1024 * 1024 >= conf["max_bytes"]:
        return {
            "ok": False,
            "issues": [
                f"图库已达上限（{status['remaining_files']}/{conf['max_files']} 张，"
                f"{status['remaining_mb']:.1f}/{conf['max_bytes'] / 1024 / 1024:.0f} MB），请先清理"
            ],
            "stats": stats(db),
        }
    persona = _persona_row(db, persona_id) if persona_id else None
    found = await search(db, query, persona_id=persona_id, limit=limit * 4)
    if not found.get("ok"):
        return found
    root = media_dir()
    saved: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=35.0, follow_redirects=True) as client:
        for raw in found.get("items") or []:
            if external_id and raw.get("external_id") != external_id:
                continue
            if raw.get("downloaded"):
                continue
            item = media_mod.MediaItem(
                external_id=raw["external_id"],
                source=raw["source"],
                title=raw["title"],
                tags=raw.get("tags") or [],
                license=raw["license"],
                author=raw["author"],
                page_url=raw["page_url"],
                thumb_url=raw.get("thumb_url") or "",
                image_url=raw.get("thumb_url") or "",
            )
            path = await media_mod.download_image(client, item, root)
            if not path:
                continue
            try:
                with db._lock, db.conn:  # noqa: SLF001
                    db.conn.execute(
                        "INSERT INTO media_library (external_id, source, path, title, tags,"
                        " license, author, source_url, fetched_at, used_count, persona_id)"
                        " VALUES (?,?,?,?,?,?,?,?,?,1,?)"
                        " ON CONFLICT(external_id) DO UPDATE SET used_count=used_count+1,"
                        " persona_id=COALESCE(NULLIF(excluded.persona_id,''), persona_id)",
                        (
                            item.external_id,
                            item.source,
                            path,
                            item.title,
                            json.dumps(list(item.tags or []), ensure_ascii=False),
                            item.license,
                            item.author,
                            item.page_url,
                            time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                            persona_id,
                        ),
                    )
            except sqlite3.Error:
                pass
            saved.append(
                {
                    "path": path,
                    "title": item.title,
                    "source": item.source,
                    "license": item.license,
                    "author": item.author,
                    "thumb_url": item.thumb_url,
                }
            )
            if len(saved) >= limit:
                break

    post: dict[str, Any] | None = None
    if with_caption and saved and persona is not None:
        post = await _post_photo(db, llm, persona, saved[0])
    return {"ok": True, "query": query, "saved": saved, "post": post, "stats": stats(db)}


async def _post_photo(db: Any, llm: Any, persona: dict[str, Any], image: dict[str, Any]) -> dict[str, Any] | None:
    """用一张图给人物发一条配文动态（LLM 按图写文案；失败则跳过）。"""
    from app.admin import persona_studio as ps  # noqa: PLC0415

    caption = ""
    for _try in range(2):
        try:
            caption = await ps._llm_caption(llm, persona, image)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            caption = ""
        if caption:
            break
    if not caption:
        return None
    import uuid
    from datetime import datetime, timedelta, timezone

    created = datetime.now(timezone.utc) - timedelta(minutes=int(time.time() % 120) + 5)
    source_ref = {
        "kind": "stock_photo",
        "caption": "llm",
        "author": str(image.get("author") or ""),
        "license": str(image.get("license") or ""),
        "url": str(image.get("thumb_url") or ""),
    }
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at, expires_at,"
                " post_kind, alt_text, source_ref)"
                " VALUES (?,?,?,?, '[]', ?, 'public', 0, 0, ?, '', 'life_photo', ?, ?)",
                (
                    f"pp_{uuid.uuid4().hex[:12]}",
                    str(persona["id"]),
                    caption[:400],
                    json.dumps([image["path"]], ensure_ascii=False),
                    str(persona.get("language") or "en")[:8],
                    created.isoformat(timespec="seconds"),
                    str(image.get("title") or "")[:200],
                    json.dumps(source_ref, ensure_ascii=False, separators=(",", ":")),
                ),
            )
    except sqlite3.Error:
        return None
    return {"persona_id": persona["id"], "content": caption, "image": image["path"]}


async def fill(
    db: Any,
    llm: Any,
    *,
    persona_ids: list[str] | None = None,
    per_persona: int = 2,
    with_caption: bool = True,
) -> dict[str, Any]:
    """按角色兴趣补图：目标 = 每个角色拥有 ``per_persona`` 条带图动态。

    - 每批最多处理 8 个「带图动态数还没达标」的角色；
    - 已经下载过图（media_library.persona_id 记录）但上次配文失败的：复用那张图重试，
      不重复下载；
    - 配文成功才写动态；``remaining_personas`` = 还没达标的角色数（前端循环用）。
    """
    target = max(1, min(int(per_persona), 3))
    status = prune(db)
    conf = limits()
    if status["remaining_files"] >= conf["max_files"] or status["remaining_mb"] * 1024 * 1024 >= conf["max_bytes"]:
        return {"ok": False, "issues": ["图库已达上限，请先清理"], "stats": status}
    with db._lock:  # noqa: SLF001
        if persona_ids:
            rows = [
                dict(r)
                for r in db.conn.execute(
                    "SELECT * FROM personas WHERE status='active' AND id IN ("
                    + ",".join("?" * len(persona_ids))
                    + ")",
                    tuple(persona_ids),
                ).fetchall()
            ]
        else:
            rows = [
                dict(r)
                for r in db.conn.execute(
                    "SELECT * FROM personas WHERE status='active' ORDER BY created_at"
                ).fetchall()
            ]
        counts = {
            str(r["persona_id"]): int(r["n"])
            for r in db.conn.execute(
                "SELECT persona_id, COUNT(*) AS n FROM persona_posts"
                " WHERE media<>'[]' AND media<>'' GROUP BY persona_id"
            ).fetchall()
        }
        used_paths: set[str] = set()
        for post_row in db.conn.execute(
            "SELECT media FROM persona_posts WHERE media<>'[]' AND media<>''"
        ).fetchall():
            try:
                for p in json.loads(str(post_row["media"] or "[]")):
                    if str(p):
                        used_paths.add(str(p))
            except (TypeError, ValueError):
                pass
        linked_rows = [
            dict(r)
            for r in db.conn.execute(
                "SELECT path, title, source, license, author, persona_id FROM media_library"
                " WHERE persona_id<>'' ORDER BY fetched_at DESC"
            ).fetchall()
        ]
    linked: dict[str, dict[str, Any]] = {}
    for row in linked_rows:
        pid = str(row.get("persona_id") or "")
        if pid and pid not in linked and str(row.get("path") or "") not in used_paths:
            linked[pid] = row
    files = _scan_files(media_dir())
    todo = [r for r in rows if counts.get(str(r.get("id")), 0) < target]
    todo.sort(key=lambda r: (counts.get(str(r.get("id")), 0), 0 if str(r.get("id")) in linked else 1))
    todo = todo[:8]
    images_added = 0
    images_reused = 0
    posted = 0
    skipped = 0
    caption_failed = 0
    samples: list[dict[str, Any]] = []
    for persona in todo:
        pid = str(persona.get("id") or "")
        got: dict[str, Any] | None = None
        link = linked.get(pid)
        if link and str(link.get("path") or "") in files:
            got = {
                "path": str(link["path"]),
                "title": str(link.get("title") or ""),
                "source": str(link.get("source") or ""),
                "license": str(link.get("license") or ""),
                "author": str(link.get("author") or ""),
                "thumb_url": "",
            }
            images_reused += 1
        else:
            for query in media_mod.queries_for(persona):
                try:
                    res = await fetch(db, query, persona_id=pid, limit=1, llm=llm)
                except Exception:  # noqa: BLE001
                    continue
                if res.get("ok") and res.get("saved"):
                    got = res["saved"][0]
                    images_added += 1
                    break
        if not got:
            skipped += 1
            continue
        if with_caption:
            post = await _post_photo(db, llm, persona, got)
            if post:
                posted += 1
                if len(samples) < 5:
                    samples.append(post)
            else:
                caption_failed += 1
    remaining = 0
    try:
        with db._lock:  # noqa: SLF001
            total = int(
                db.conn.execute("SELECT COUNT(*) FROM personas WHERE status='active'").fetchone()[0]
            )
        after_counts = {}
        with db._lock:  # noqa: SLF001
            for row in db.conn.execute(
                "SELECT persona_id, COUNT(*) AS n FROM persona_posts"
                " WHERE media<>'[]' AND media<>'' GROUP BY persona_id"
            ).fetchall():
                after_counts[str(row["persona_id"])] = int(row["n"])
        remaining = sum(1 for _pid in (str(r.get("id")) for r in rows) if after_counts.get(_pid, 0) < target)
        if not persona_ids:
            pass
    except sqlite3.Error:
        remaining = max(0, len(rows) - len(todo))
    return {
        "ok": True,
        "target_per_persona": target,
        "personas": len(todo),
        "images_added": images_added,
        "images_reused": images_reused,
        "posts_created": posted,
        "caption_failed": caption_failed,
        "skipped": skipped,
        "samples": samples,
        "stats": stats(db),
        "remaining_personas": remaining,
    }



def list_library(
    db: Any, *, offset: int = 0, limit: int = 60, only_unused: bool = False
) -> dict[str, Any]:
    """列出图库中真实存在的图片（分页），标注是否已被动态引用、来源人物。"""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 120))
    files = _scan_files(media_dir())
    referenced = _referenced_paths(db)
    try:
        with db._lock:  # noqa: SLF001
            rows = [
                dict(r)
                for r in db.conn.execute(
                    "SELECT path, title, source, license, author, source_url,"
                    " fetched_at, persona_id FROM media_library"
                    " ORDER BY fetched_at DESC, path"
                ).fetchall()
            ]
            personas = {
                str(r["id"]): str(r["nickname"] or "")
                for r in db.conn.execute("SELECT id, nickname FROM personas").fetchall()
            }
    except sqlite3.Error:
        return {"ok": False, "issues": ["数据库未就绪"]}
    items: list[dict[str, Any]] = []
    for row in rows:
        path = str(row.get("path") or "")
        if path not in files:
            continue
        is_ref = path in referenced
        if only_unused and is_ref:
            continue
        persona_id = str(row.get("persona_id") or "")
        items.append(
            {
                "path": path,
                "url": path,
                "title": str(row.get("title") or ""),
                "source": str(row.get("source") or ""),
                "license": str(row.get("license") or ""),
                "author": str(row.get("author") or ""),
                "fetched_at": str(row.get("fetched_at") or ""),
                "persona_id": persona_id,
                "persona_nickname": personas.get(persona_id, ""),
                "referenced": is_ref,
                "size": int(files[path]["size"]),
            }
        )
    total = len(items)
    return {
        "ok": True,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items[offset : offset + limit],
        "stats": {
            "files": len(files),
            "referenced_files": sum(1 for i in items if i["referenced"]),
            "unreferenced_files": sum(1 for i in items if not i["referenced"]),
        },
    }



def person_ids_without_media(db: Any) -> int:
    try:
        with db._lock:  # noqa: SLF001
            total = int(
                db.conn.execute("SELECT COUNT(*) FROM personas WHERE status='active'").fetchone()[0]
            )
            have = int(
                db.conn.execute(
                    "SELECT COUNT(DISTINCT persona_id) FROM persona_posts"
                    " WHERE media<>'[]' AND media<>''"
                ).fetchone()[0]
            )
    except sqlite3.Error:
        return 0
    return max(0, total - have)


async def janitor_loop(db: Any, *, interval_s: float = 3600.0) -> None:
    """后台图库清理：启动先跑一次，之后每小时。"""
    import asyncio  # noqa: PLC0415

    interval = max(60.0, float(interval_s or 3600.0))
    while True:
        try:
            result = prune(db)
            if result.get("deleted_files") or result.get("removed_missing_rows"):
                log.info("图库清理：%s", result)
        except Exception as exc:  # noqa: BLE001 - 后台任务不能拖垮服务
            log.warning("图库清理失败（下轮重试）: %s", exc)
        await asyncio.sleep(interval)


__all__ = [
    "fetch",
    "fill",
    "janitor_loop",
    "list_library",
    "limits",
    "media_dir",
    "person_ids_without_media",
    "prune",
    "search",
    "stats",
]
