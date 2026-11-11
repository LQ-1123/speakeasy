"""V3 · 媒体资产归属 + 短时媒体令牌（多用户数据隔离）。

背景
----
多用户升级第一期要做「数据隔离」：只拿到别人的音频/图片文件名，不能直接读。
本模块提供最小可用的媒体归属登记 + 无状态短时令牌：

- ``media_assets`` 表记录「某个文件属于谁」，文件在**合成 / 上传落盘时**登记；
- :func:`issue_media_token` / :func:`verify_media_token` 是基于 HMAC-SHA256 的
  无状态 capability token：payload 含 ``user`` / ``kind`` / ``filename`` / ``exp``，
  服务端只校验签名与过期时间，不落库。这样 ``<audio src="/api/v2/audio/x.wav?t=...">``
  这种带不了自定义请求头的场景也能做一次性授权。

密钥
----
``SE_MEDIA_SECRET`` 环境变量；未配置时使用**固定的开发默认值**并打 warning。
生产环境必须配置，否则任何人都能用公开的默认密钥伪造 token。

兼容策略（重要）
----------------
隔离上线前就存在的历史音频/图片没有 ``media_assets`` 记录。读取端必须继续放行，
否则老用户的历史聊天记录会全部变哑；这类文件走「legacy」分支，响应头会加
``X-Legacy-Asset: 1``，方便后续做存量回填与灰度下线。**新产生的私聊资产必须在
落盘处登记**，否则就绕过了隔离。

文件名约定
----------
- ``audio``：裸文件名（``data/audio`` 下的 basename），由 ``AudioStore.path_for`` 保证无目录；
- ``chat_image`` / ``post_image``：``data/uploads`` 下的文件名；v3 上传图片会带
  ``<user_hash>/`` 子目录，因此登记的是相对 ``/uploads/`` 的路径（含子目录）。
:func:`normalize_filename` 统一去掉 ``/uploads/`` 前缀并拒绝目录穿越。
"""

from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 媒体类型常量（调用方统一用这些字符串，避免各写各的）
KIND_AUDIO = "audio"
KIND_CHAT_IMAGE = "chat_image"
KIND_POST_IMAGE = "post_image"
KIND_PUBLIC_AUDIO = "public_audio"  # 人物公共朗读：登记了也不校验

#: 读取端会校验归属的 kind（/api/v2/audio 与 /api/v2/media 各自使用子集）
MEDIA_KINDS = (KIND_AUDIO, KIND_CHAT_IMAGE, KIND_POST_IMAGE)

_SECRET_ENV = "SE_MEDIA_SECRET"
#: 开发期默认密钥。仅用于「没配 SE_MEDIA_SECRET 的本地单机」，生产必须覆盖。
_DEV_SECRET = "sonder-dev-media-secret-change-me"

_TOKEN_VERSION = "v1"
_MAX_FILENAME_LEN = 512
_MAX_TTL_S = 3600
_DEFAULT_TTL_S = 300

#: 已确保建表的 DB（弱引用，DB 对象被回收后自动移除）
_schema_ready: "weakref.WeakSet[Any]" = weakref.WeakSet()
_warned_default_secret = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_filename(filename: str) -> str:
    """规范化媒体文件名，非法时返回空串。

    - 统一 ``\\`` 为 ``/``；
    - 去掉 ``/uploads/``、``uploads/``、前导 ``/``；
    - 拒绝空串、``..`` 目录穿越、隐藏文件、超长名。
    """
    raw = str(filename or "").strip().replace("\\", "/")
    for prefix in ("/uploads/", "uploads/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    raw = raw.lstrip("/")
    if not raw or len(raw) > _MAX_FILENAME_LEN:
        return ""
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in (".", "..") or part.startswith(".") for part in parts):
        return ""
    return "/".join(parts)


def _normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if not value or len(value) > 32:
        return ""
    if not all(ch.isalnum() or ch in "_-" for ch in value):
        return ""
    return value


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def ensure_media_schema(db: Any) -> None:
    """幂等创建 ``media_assets`` 表（可重复调用）。"""
    try:
        if db in _schema_ready:  # 快路径：同一次进程内只查一遍
            return
    except TypeError:  # pragma: no cover - 非 weakref-able 的 DB 替身
        pass
    with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层共用同一把锁/连接
        db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_assets (
                asset_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(kind, filename)
            )
            """
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_assets_owner"
            " ON media_assets(owner_user_id)"
        )
    try:
        _schema_ready.add(db)
    except TypeError:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# 归属登记 / 查询
# --------------------------------------------------------------------------- #
def register_asset(
    db: Any, owner_user_id: str, kind: str, filename: str
) -> dict[str, str] | None:
    """登记一个媒体文件的归属，返回登记行（已存在则返回已有行）。

    语义是「第一次登记为准」（``INSERT OR IGNORE``）：同一个 ``(kind, filename)``
    不允许被后来的用户改写归属，避免越权重登记。参数非法时抛 ``ValueError``。
    """
    owner = str(owner_user_id or "").strip()
    k = _normalize_kind(kind)
    name = normalize_filename(filename)
    if not owner or not k or not name:
        raise ValueError("register_asset: owner/kind/filename 非法")

    ensure_media_schema(db)
    asset_id = "ma_" + uuid.uuid4().hex
    created_at = _now_iso()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT OR IGNORE INTO media_assets"
            " (asset_id, owner_user_id, kind, filename, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (asset_id, owner, k, name, created_at),
        )
        row = db.conn.execute(
            "SELECT asset_id, owner_user_id, kind, filename, created_at"
            " FROM media_assets WHERE kind=? AND filename=?",
            (k, name),
        ).fetchone()
    return dict(row) if row is not None else None


def owner_of(db: Any, kind: str, filename: str) -> str | None:
    """查询已登记文件的 owner；未登记（历史文件）返回 ``None``。"""
    k = _normalize_kind(kind)
    name = normalize_filename(filename)
    if not k or not name:
        return None
    ensure_media_schema(db)
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT owner_user_id FROM media_assets WHERE kind=? AND filename=?",
            (k, name),
        ).fetchone()
    return str(row[0]) if row is not None and row[0] else None


def owner_of_filename(
    db: Any, filename: str, kinds: tuple[str, ...] | None = None
) -> tuple[str, str] | None:
    """按文件名在所有（或指定）媒体类型里查归属，返回 ``(owner, kind)``。

    ``/api/v2/media/{filename}`` 的 URL 不带 kind，只能按文件名反查。
    """
    name = normalize_filename(filename)
    if not name:
        return None
    ensure_media_schema(db)
    sql = "SELECT owner_user_id, kind FROM media_assets WHERE filename=?"
    params: list[Any] = [name]
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        sql += f" AND kind IN ({placeholders})"
        params.extend(_normalize_kind(k) for k in kinds)
    sql += " ORDER BY created_at ASC LIMIT 1"
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(sql, tuple(params)).fetchone()
    if row is None or not row[0]:
        return None
    return str(row[0]), str(row[1])


# --------------------------------------------------------------------------- #
# 短时媒体令牌（HMAC-SHA256，无状态）
# --------------------------------------------------------------------------- #
def _secret() -> bytes:
    global _warned_default_secret
    raw = os.getenv(_SECRET_ENV, "").strip()
    if raw:
        return raw.encode("utf-8")
    if not _warned_default_secret:
        log.warning(
            "未配置 %s，媒体令牌使用固定开发默认密钥；生产环境必须设置该环境变量",
            _SECRET_ENV,
        )
        _warned_default_secret = True
    return _DEV_SECRET.encode("utf-8")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign(body: str) -> str:
    digest = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def issue_media_token(
    db: Any, user_id: str, kind: str, filename: str, ttl_s: int = _DEFAULT_TTL_S
) -> str:
    """为 ``(user_id, kind, filename)`` 签发短时令牌。

    ``db`` 目前只用于未来可能的吊销/审计扩展，不参与签名；令牌是纯无状态凭证。
    返回 ``v1.<payload>.<signature>``。
    """
    del db  # 预留参数：归属校验由调用方（路由）负责，令牌本身无状态
    user = str(user_id or "").strip()
    k = _normalize_kind(kind)
    name = normalize_filename(filename)
    if not user or not k or not name:
        raise ValueError("issue_media_token: user/kind/filename 非法")
    try:
        ttl = int(ttl_s)
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL_S
    ttl = max(1, min(ttl, _MAX_TTL_S))
    payload = {
        "v": 1,
        "user": user,
        "kind": k,
        "filename": name,
        "exp": int(time.time()) + ttl,
    }
    body = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
    )
    return f"{_TOKEN_VERSION}.{body}.{_sign(body)}"


def verify_media_token(db: Any, token: str) -> tuple[str, str, str] | None:
    """校验令牌，成功返回 ``(user_id, kind, filename)``，失败返回 ``None``。

    校验内容：版本前缀、HMAC 签名（常量时间比较）、``exp`` 未过期、payload 字段合法。
    """
    del db  # 预留参数：与 issue_media_token 对称；令牌无状态
    parts = str(token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        return None
    body, signature = parts[1], parts[2]
    if not body or not signature:
        return None
    try:
        expected = _sign(body)
    except (UnicodeEncodeError, ValueError):
        return None
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    user = str(payload.get("user") or "").strip()
    kind = _normalize_kind(str(payload.get("kind") or ""))
    name = normalize_filename(str(payload.get("filename") or ""))
    try:
        exp = int(payload.get("exp"))
    except (TypeError, ValueError):
        return None
    if not user or not kind or not name:
        return None
    if exp <= int(time.time()):
        return None
    return user, kind, name


# --------------------------------------------------------------------------- #
# 上传落盘处的「兜底登记」
# --------------------------------------------------------------------------- #
class _SqliteDB:
    """最小 DB 适配器：media_* 只需要 ``conn`` + ``_lock``。"""

    __slots__ = ("conn", "_lock")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()


def _find_app_db(data_dir: str) -> str | None:
    """在数据目录里定位应用 SQLite（上传调用方没有 db 句柄时的兜底）。

    约定优先 ``<data>/speakeasy_v2.db``（``build_services`` 的落盘名）；找不到就扫
    同目录下的 ``*.db``，取第一个含 ``friends`` 表（即应用库）的文件。都找不到返回 None。
    """
    preferred = os.path.join(data_dir, "speakeasy_v2.db")
    if os.path.isfile(preferred):
        return preferred
    for candidate in sorted(glob.glob(os.path.join(data_dir, "*.db"))):
        try:
            conn = sqlite3.connect(candidate, timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='friends'"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if row is not None:
            return candidate
    return None


def register_uploaded_asset(
    uploads_root: str,
    user_id: str,
    relative_path: str,
    *,
    kind: str = KIND_POST_IMAGE,
    db: Any | None = None,
) -> bool:
    """登记「用户上传图片」的归属，返回是否登记成功。

    ``/api/v3/media/upload`` 的落盘在 :mod:`app.v3.uploads`，但该路由当前没有把
    DB 句柄传下来（``save_user_image(raw, user_id, uploads_root)``）。为了不跨文件
    改路由，这里做**尽力而为**的兜底：调用方给了 ``db`` 就用；没给就从
    ``uploads_root`` 的父目录定位应用库。任何失败只记 debug，绝不影响上传本身。
    """
    name = normalize_filename(relative_path)
    if not name:
        return False
    try:
        if db is not None:
            register_asset(db, user_id, kind, name)
            return True
        data_dir = os.path.dirname(os.path.abspath(str(uploads_root or "")))
        if not data_dir or not os.path.isdir(data_dir):
            return False
        db_path = _find_app_db(data_dir)
        if not db_path:
            log.debug("上传资产未登记（找不到应用库）: %s", relative_path)
            return False
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            register_asset(_SqliteDB(conn), user_id, kind, name)
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - 归属登记失败不能让上传失败
        log.warning("上传资产归属登记失败 %s: %s: %s", relative_path, type(exc).__name__, exc)
        return False


def signed_url(
    db: Any, user_id: str, url: Any, *, ttl_s: int = 3600,
) -> str:
    """给「属于当前用户」的媒体 URL 追加 ``?t=<短时令牌>``。

    浏览器 ``<img src>`` 无法携带 ``X-User-*``/会话头，而 ``/uploads/*`` 与
    ``/api/v2/media/*`` 已按归属校验；序列化响应时由服务端直接签名，前端无需改造。

    - 未登记 / 非本人 / 不是站内媒体 URL：原样返回；
    - ``/uploads/...`` → ``kind='post_image'``；``/api/v2/media/...`` → ``kind='chat_image'``。
    """
    raw = str(url or "").strip()
    if not raw or not str(user_id or "").strip():
        return raw
    for prefix, kind in (("/uploads/", KIND_POST_IMAGE), ("/api/v2/media/", KIND_CHAT_IMAGE)):
        if not raw.startswith(prefix):
            continue
        name = normalize_filename(raw[len(prefix):])
        if not name:
            return raw
        owner = owner_of(db, kind, name)
        if owner is None or owner != str(user_id):
            return raw
        token = issue_media_token(db, str(user_id), kind, name, ttl_s)
        sep = "&" if "?" in raw else "?"
        return f"{raw}{sep}t={token}"
    return raw


def signed_media_list(db: Any, user_id: str, media: Any) -> list:
    """给动态媒体数组里的 URL 逐个签名（保持原始结构：字符串或 ``{url:...}``）。"""
    items = media if isinstance(media, list) else []
    out: list = []
    for item in items:
        if isinstance(item, str):
            out.append(signed_url(db, user_id, item))
        elif isinstance(item, dict) and item.get("url"):
            data = dict(item)
            data["url"] = signed_url(db, user_id, data.get("url"))
            out.append(data)
        else:
            out.append(item)
    return out


__all__ = [
    "KIND_AUDIO",
    "KIND_CHAT_IMAGE",
    "KIND_POST_IMAGE",
    "KIND_PUBLIC_AUDIO",
    "MEDIA_KINDS",
    "ensure_media_schema",
    "issue_media_token",
    "normalize_filename",
    "owner_of",
    "owner_of_filename",
    "register_asset",
    "register_uploaded_asset",
    "verify_media_token",
]
