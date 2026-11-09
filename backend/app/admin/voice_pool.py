"""Fish 音色池（管理台「音色池」页签的后端）。

- 手动输入 Fish 模型/音色 id → 真实合成一句做检测 → 加入素材池；
- 素材池持久化在 ``data/fish_voice_pool.json``；
- 捏人（``ensure_persona_voices`` / 每日世界）优先从素材池挑，其次才用 Fish 官方音色库；
- 支持列出 Fish 账号下**全部**音色（分页拉取），并逐个试听（合成短句缓存到 data/voice_previews）。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

POOL_FILENAME = "fish_voice_pool.json"
PREVIEW_DIRNAME = "voice_previews"
PREVIEW_TTL = 7 * 24 * 3600

#: 检测/试听用的短句（按语言选一句，Fish 多语言音色都能读）
_SAMPLES = {
    "zh": "你好，很高兴认识你。我们一起练口语吧。",
    "ja": "こんにちは。一緒に会話の練習をしましょう。",
    "ko": "안녕하세요. 같이 회화 연습을 해요.",
    "es": "Hola, encantado de conocerte. Practiquemos juntos.",
    "fr": "Bonjour, ravi de te rencontrer. Pratiquons ensemble.",
    "de": "Hallo, schön dich kennenzulernen. Lass uns zusammen üben.",
    "pt": "Olá, prazer em conhecer você. Vamos praticar juntos.",
    "it": "Ciao, piacere di conoscerti. Facciamo pratica insieme.",
    "ar": "مرحباً، سعيد بلقائك. لنتدرب معاً.",
    "ru": "Привет, приятно познакомиться. Давай практиковаться вместе.",
    "en": "Hi! Nice to meet you. Let's practice English together.",
}


def _safe_id(reference_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", reference_id.strip())[:120] or "voice"


def _data_root() -> Path:
    from app.config import get_settings  # noqa: PLC0415

    return Path(str(get_settings().data_path))


def _pool_path() -> Path:
    return _data_root() / POOL_FILENAME


def _cfg() -> Any:
    from app.voice.tts_config import TTSConfigStore  # noqa: PLC0415

    return TTSConfigStore(str(_data_root())).load()


def _fish_key_and_base() -> tuple[str, str, str]:
    cfg = _cfg()
    if str(getattr(cfg, "provider", "") or "").strip().lower() != "fish":
        raise RuntimeError("当前 TTS provider 不是 fish（管理台「模型与密钥」里切换并保存）")
    key = str(getattr(cfg, "fish_api_key", "") or "").strip()
    if not key:
        raise RuntimeError("未配置 Fish API Key")
    base = str(getattr(cfg, "fish_base_url", "https://api.fish.audio") or "").strip()
    model = str(getattr(cfg, "fish_model", "s2.1-pro") or "s2.1-pro").strip()
    return key, base, model


# --------------------------------------------------------------------------- #
# 素材池持久化
# --------------------------------------------------------------------------- #
def load_pool() -> list[dict[str, Any]]:
    path = _pool_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    voices = raw.get("voices") if isinstance(raw, dict) else raw
    out: list[dict[str, Any]] = []
    for item in voices or []:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("reference_id") or "").strip()
        if not rid:
            continue
        out.append(
            {
                "reference_id": rid,
                "name": str(item.get("name") or "").strip() or rid[:12],
                "gender": str(item.get("gender") or "").strip().lower(),
                "language": str(item.get("language") or "").strip().lower(),
                "source": str(item.get("source") or "manual"),
                "added_at": str(item.get("added_at") or ""),
            }
        )
    return out


def save_pool(voices: list[dict[str, Any]]) -> None:
    path = _pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    body = {"voices": voices, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def add_voice(
    reference_id: str,
    *,
    name: str = "",
    gender: str = "",
    language: str = "",
    source: str = "manual",
) -> dict[str, Any]:
    rid = str(reference_id or "").strip()
    if not rid:
        return {"ok": False, "issues": ["reference_id 不能为空"]}
    pool = load_pool()
    for item in pool:
        if item["reference_id"] == rid:
            item["name"] = str(name or "").strip() or item["name"]
            item["gender"] = str(gender or "").strip().lower() or item["gender"]
            item["language"] = str(language or "").strip().lower() or item["language"]
            save_pool(pool)
            return {"ok": True, "updated": True, "voices": pool}
    pool.append(
        {
            "reference_id": rid,
            "name": str(name or "").strip() or rid[:12],
            "gender": str(gender or "").strip().lower(),
            "language": str(language or "").strip().lower(),
            "source": str(source or "manual"),
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    save_pool(pool)
    return {"ok": True, "updated": False, "voices": pool}


def remove_voice(reference_id: str) -> dict[str, Any]:
    rid = str(reference_id or "").strip()
    pool = [item for item in load_pool() if item["reference_id"] != rid]
    save_pool(pool)
    return {"ok": True, "voices": pool}


def resolve(query: str) -> dict[str, Any] | None:
    """把「id 或名称」解析成素材池里的音色。

    支持：完整 reference_id → 精确名称（忽略大小写）→ 唯一的名称子串。
    找不到返回 None（调用方可以把它当原始 id 继续用）。
    """
    q = str(query or "").strip()
    if not q:
        return None
    pool = load_pool()
    for item in pool:
        if item["reference_id"] == q:
            return dict(item)
    ql = q.casefold()
    for item in pool:
        if item["name"].casefold() == ql:
            return dict(item)
    if len(ql) >= 2:
        hits = [item for item in pool if ql in item["name"].casefold()]
        if len(hits) == 1:
            return dict(hits[0])
    return None


def resolve_id(query: str) -> str:
    """解析成 reference_id；解析不到就原样返回（当成手输的 id）。"""
    hit = resolve(query)
    return hit["reference_id"] if hit else str(query or "").strip()


# --------------------------------------------------------------------------- #
# 音色挑选（捏人时优先素材池）
# --------------------------------------------------------------------------- #
def pool_entries() -> list[dict[str, Any]]:
    """把素材池转成 seed_data 音色池格式（``id`` / ``languages`` / ``tags``）。"""
    entries: list[dict[str, Any]] = []
    for item in load_pool():
        entries.append(
            {
                "id": item["reference_id"],
                "languages": [item["language"]] if item["language"] else [],
                "tags": [item["gender"]] if item["gender"] else [],
            }
        )
    return entries


def pick_from_pool(
    language: str, gender: str, used: set[str] | None = None, *, randomize: bool = False
) -> str:
    """按 语言+性别 → 性别 → 任意 的优先级从素材池挑一个（尽量避开已占用）。"""
    used = used or set()
    pool = load_pool()
    if not pool:
        return ""
    lang = str(language or "").strip().lower()
    g = str(gender or "").strip().lower()

    def match(need_lang: bool, need_g: bool) -> list[str]:
        out = []
        for item in pool:
            if need_lang and lang and item["language"] != lang:
                continue
            if need_g and g and item["gender"] != g:
                continue
            out.append(item["reference_id"])
        return out

    for need_lang, need_g in ((True, True), (False, True), (False, False)):
        candidates = [rid for rid in match(need_lang, need_g) if rid not in used]
        if candidates:
            return random.choice(candidates) if randomize else candidates[0]
    return ""


# --------------------------------------------------------------------------- #
# 试听（真实合成 + 本地缓存）
# --------------------------------------------------------------------------- #
async def synth_sample(reference_id: str, language: str = "en") -> bytes:
    from app.voice import fish_tts  # noqa: PLC0415

    key, base, model = _fish_key_and_base()
    lang = (str(language or "").strip().lower()[:2]) or "en"
    text = _SAMPLES.get(lang) or _SAMPLES["en"]
    return await fish_tts.synthesize(
        text,
        api_key=key,
        base_url=base,
        model=model,
        reference_id=reference_id,
        audio_format="mp3",
        timeout=60.0,
    )


async def preview_audio(reference_id: str, *, refresh: bool = False) -> tuple[bytes, str]:
    rid = str(reference_id or "").strip()
    if not rid:
        raise RuntimeError("reference_id 不能为空")
    lang = "en"
    for item in load_pool():
        if item["reference_id"] == rid and item["language"]:
            lang = item["language"]
            break
    cache_dir = _data_root() / PREVIEW_DIRNAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{hashlib.sha1(rid.encode()).hexdigest()[:16]}.mp3"
    if not refresh and path.exists() and (time.time() - path.stat().st_mtime) < PREVIEW_TTL:
        return path.read_bytes(), "audio/mpeg"
    data = await synth_sample(rid, lang)
    path.write_bytes(data)
    return data, "audio/mpeg"


# --------------------------------------------------------------------------- #
# Fish 账号音色列表 / 状态
# --------------------------------------------------------------------------- #
async def list_all_models(*, refresh: bool = False, max_pages: int = 10) -> list[dict[str, Any]]:
    from app.voice import fish_tts  # noqa: PLC0415

    key, base, _model = _fish_key_and_base()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        rows = await fish_tts.list_models(
            api_key=key,
            base_url=base,
            page_number=page,
            page_size=100,
            use_cache=not refresh,
            timeout=25.0,
        )
        for row in rows:
            rid = str(row.get("id") or "").strip()
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append(row)
        if len(rows) < 100:
            break
    return out


def _assigned_counts(db: Any) -> dict[str, dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT fish_voice_id, COUNT(*) AS n FROM personas"
                " WHERE COALESCE(fish_voice_id, '') != '' GROUP BY fish_voice_id"
            ).fetchall()
        for row in rows:
            rid = str(row[0])
            counts[rid] = {"count": int(row[1] or 0)}
    except Exception:  # noqa: BLE001
        return counts
    return counts


def status(db: Any = None) -> dict[str, Any]:
    from app.voice import voice_registry  # noqa: PLC0415

    cfg = None
    try:
        cfg = _cfg()
    except Exception:  # noqa: BLE001
        pass
    assigned = _assigned_counts(db) if db is not None else {}
    gaps = {"waiting_voice": 0, "gaps": []}
    if db is not None:
        from app.voice.catalog import voice_gaps

        gaps = voice_gaps(db)
    pool = []
    for item in load_pool():
        row = dict(item)
        row["assigned"] = int((assigned.get(item["reference_id"]) or {}).get("count") or 0)
        pool.append(row)
    return {
        "fish": {
            "configured": voice_registry.fish_configured(cfg),
            "usable": voice_registry.fish_usable(cfg),
            "status": voice_registry.fish_status(),
        },
        "voices": pool,
        "assigned": assigned,
        "voice_gaps": gaps,
    }


__all__ = [
    "add_voice",
    "list_all_models",
    "load_pool",
    "pick_from_pool",
    "pool_entries",
    "preview_audio",
    "remove_voice",
    "status",
    "synth_sample",
]
