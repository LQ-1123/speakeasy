"""管理 API · 图片库（``/api/admin/media/*``）。

从 ``app/admin/router.py`` 原样迁移的路径，**路径与返回结构完全不变**：
``/media/stats`` / ``/media/library`` / ``/media/search`` / ``/media/fetch``
``/media/fill`` / ``/media/prune``。

第 3 期（P3）新增：``POST /media/upload``（multipart 上传本地图片，落
``media_dir`` + ``media_library``）。上传校验三条：

1. 只收图片 mime（jpg/png/webp/gif），并用 Pillow **真实解码**验证，
   防止改后缀的任意文件混进图库；
2. 大小上限复用 ``media_admin.limits()["download_max_bytes"]``（与下载同一口径）；
3. **文件名由服务端重命名**（``upload_<uuid>.<ext>``），绝不使用上传时的原始
   文件名——原始名可能含 ``../`` 或 ``\\``，是路径穿越的直接入口；原始名只作为
   ``media_library.title`` 里的展示文本保存（去目录、截断）。

数据库未就绪时沿用旧约定：返回 ``{"ok": False, "issues": ["数据库未就绪"]}`` 配 200。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, File, Form, Request, UploadFile

from app.admin import media_admin
from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: ``tags`` 不在此声明：父路由 ``app/admin/router.py`` 已带 ``tags=["admin"]``，
#: 两层重复声明会让每条路由的 OpenAPI tags 变成 ``["admin", "admin"]``——
#: 虽然无害，但会改变旧路径的接口签名（拆分前后应逐字节一致）。
router = APIRouter(prefix="/media")

#: 允许上传的图片 mime（客户端声明值，仍要用 Pillow 复核真实内容）。
ALLOWED_IMAGE_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)

#: Pillow 识别出的真实格式 → 落盘扩展名（只认这四种，其余一律拒绝）。
_PIL_FORMAT_EXT: dict[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
}

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

def _safe_title(filename: Any) -> str:
    """原始文件名 → 仅用于展示的 title（去掉目录部分并截断，不参与任何路径拼接）。"""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:120]

def _detect_extension(data: bytes) -> str:
    """用 Pillow 真实解码判断格式；不是有效图片返回空串。"""
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            return _PIL_FORMAT_EXT.get(str(image.format or "").upper(), "")
    except Exception:  # noqa: BLE001 - 任何解码失败都视为非图片
        return ""

def _llm(request: Request) -> Any:
    """取 LLM 句柄；引擎未装配时返回 ``None``（media_admin 内部按可选处理）。"""
    engines = getattr(request.app.state, "engines", None)
    return getattr(engines, "llm", None) if engines is not None else None

@router.get("/stats")
async def admin_media_stats(request: Request) -> dict:
    _persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return media_admin.stats(db)

@router.get("/library")
async def admin_media_library(
    request: Request, offset: int = 0, limit: int = 60, only_unused: bool = False
) -> dict:
    """图库中所有已入库图片（分页，标注引用情况）。"""
    _persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return media_admin.list_library(db, offset=offset, limit=limit, only_unused=only_unused)

@router.get("/search")
async def admin_media_search(
    request: Request, query: str = "", persona_id: str = "", limit: int = 8
) -> dict:
    _persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return await media_admin.search(db, query, persona_id=persona_id, limit=limit)

@router.post("/fetch")
async def admin_media_fetch(request: Request, body: dict = Body(...)) -> dict:
    _persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return await media_admin.fetch(
        db,
        str(body.get("query") or ""),
        persona_id=str(body.get("persona_id") or ""),
        limit=int(body.get("limit") or 2),
        with_caption=bool(body.get("with_caption")),
        llm=_llm(request),
        external_id=str(body.get("external_id") or ""),
    )

@router.post("/fill")
async def admin_media_fill(request: Request, body: dict = Body(...)) -> dict:
    _persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    ids = body.get("persona_ids")
    return await media_admin.fill(
        db,
        _llm(request),
        persona_ids=[str(x) for x in ids] if isinstance(ids, list) else None,
        per_persona=int(body.get("per_persona") or 1),
        with_caption=bool(body.get("with_caption", True)),
    )

@router.post("/prune")
async def admin_media_prune(request: Request, body: dict = Body(...)) -> dict:
    _persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return media_admin.prune(
        db,
        ttl_days=int(body["ttl_days"]) if body.get("ttl_days") else None,
        max_bytes=int(body["max_bytes"]) if body.get("max_bytes") else None,
        max_files=int(body["max_files"]) if body.get("max_files") else None,
        dry_run=bool(body.get("dry_run")),
    )

@router.post("/upload")
async def admin_media_upload(
    request: Request,
    file: UploadFile = File(...),
    persona_id: str = Form(""),
) -> dict:
    """上传一张图片到图库（``multipart/form-data``：``file`` + 可选 ``persona_id``）。

    返回 ``{"ok", "path", "url", "size"}``；``path``/``url`` 都是
    ``/media/<服务端重命名后的文件名>``（与 ``GET /media/library`` 里的
    ``path`` 同源，可直接用于动态的 ``media`` 字段）。
    """
    _persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}

    conf = media_admin.limits()
    max_bytes = int(conf["download_max_bytes"])
    content_type = str(getattr(file, "content_type", "") or "").strip().lower()
    if content_type and content_type not in ALLOWED_IMAGE_TYPES:
        return {"ok": False, "issues": ["只支持 JPG / PNG / WEBP / GIF 图片"]}

    # 多读 1 字节用于判超限：不把超限的大文件整个读进内存。
    data = await file.read(max_bytes + 1)
    if not data:
        return {"ok": False, "issues": ["文件为空"]}
    if len(data) > max_bytes:
        return {
            "ok": False,
            "issues": [f"文件超过上限 {round(max_bytes / 1024 / 1024, 1)}MB"],
        }

    extension = _detect_extension(data)
    if not extension:
        return {"ok": False, "issues": ["文件不是有效图片（只支持 JPG / PNG / WEBP / GIF）"]}

    persona_value = str(persona_id or "").strip()
    if persona_value:
        with db._lock:  # noqa: SLF001
            exists = db.conn.execute(
                "SELECT 1 FROM personas WHERE id=?", (persona_value,)
            ).fetchone()
        if exists is None:
            return {"ok": False, "issues": ["角色不存在"]}

    # 服务端重命名：原名绝不参与路径拼接（防 ../ 路径穿越）
    token = uuid.uuid4().hex[:16]
    name = f"upload_{token}{extension}"
    root = media_admin.media_dir()
    path = f"/media/{name}"
    full_path = os.path.join(root, name)
    try:
        os.makedirs(root, exist_ok=True)
        with open(full_path, "wb") as handle:  # noqa: PTH123 - 明确按二进制写
            handle.write(data)
    except OSError as exc:
        log.warning("写上传文件失败: %s", exc)
        return {"ok": False, "issues": [f"写入失败：{type(exc).__name__}"]}

    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO media_library (external_id, source, path, title, tags, license,"
                " author, source_url, fetched_at, used_count, persona_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (
                    f"upload:{token}",
                    "upload",
                    path,
                    _safe_title(getattr(file, "filename", "")),
                    json.dumps([], ensure_ascii=False),
                    "",
                    "",
                    "",
                    _now_iso(),
                    persona_value,
                ),
            )
    except sqlite3.Error as exc:  # pragma: no cover - 极老库没有 media_library
        # 入库失败就把刚落盘的文件删掉，避免留下无人引用的孤儿文件
        try:
            os.remove(full_path)
        except OSError:
            pass
        log.warning("上传图片入库失败: %s", exc)
        return {"ok": False, "issues": [f"入库失败：{type(exc).__name__}"]}

    request.state.audit_action = "media.upload"
    request.state.audit_target = path
    request.state.audit_summary = (
        f"上传图片 {name}（{len(data)} 字节）persona={persona_value or '-'}"
    )
    return {"ok": True, "path": path, "url": path, "size": len(data)}

__all__ = ["ALLOWED_IMAGE_TYPES", "router"]
