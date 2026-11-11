"""V3 · 邮箱密码账号、邀请码、邮箱令牌与可撤销设备会话。

为什么单独一个模块
------------------
``security.py`` 只负责「按请求头校验身份」；真正的账号生命周期（注册、登录、
邮箱验证、找回密码、会话撤销、旧设备身份绑定、分龄能力）都在这里。路由层
(:mod:`app.v3.accounts_router`) 只做参数校验与响应组装。

数据模型（全部 ``CREATE TABLE IF NOT EXISTS``，可反复执行）
----------------------------------------------------------
``v3_accounts``            一个 user_id 一份凭据（邮箱唯一、密码哈希、验证时间、状态）
``v3_invite_codes``        邀请码（限量、有效期、可停用）
``v3_email_tokens``        邮箱验证 / 找回密码令牌（只存 sha256，一次性）
``v3_sessions``            设备会话（可撤销；token 只存 sha256）
``v3_age_verifications``   年龄核验结论（独立于可编辑资料生日）
``v3_age_audit``           核验/监护变更审计

密码哈希
--------
优先 Argon2id（``argon2-cffi``）；离线/未安装时退化为标准库 ``hashlib.scrypt``，
哈希串自带算法前缀，两种格式都能校验，后续装上 argon2-cffi 也不需要迁移。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.logging_utils import get_logger
from app.v3.identity import LOCAL_USER_ID, current_user_id

log = get_logger(__name__)

#: 会话 Cookie 名（Web 端 HttpOnly）
SESSION_COOKIE = "sonder_session"
#: CSRF 双提交 Cookie 名（非 HttpOnly，前端读出来塞进 X-CSRF-Token）
CSRF_COOKIE = "sonder_csrf"
CSRF_HEADER = "X-CSRF-Token"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{2,253}\.[A-Za-z]{2,24}$")
_MIN_PASSWORD = 8
_MAX_PASSWORD = 128
_SESSION_TTL_DAYS = 30
_TOKEN_TTL_MINUTES = 30

# --------------------------------------------------------------------------- #
# 密码哈希：Argon2id 优先，scrypt 兜底
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - 视环境是否安装 argon2-cffi
    from argon2 import PasswordHasher  # type: ignore
    from argon2.exceptions import InvalidHashError, VerifyMismatchError  # type: ignore

    _HASHER: Any = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
    HASH_ALGO = "argon2id"
except Exception:  # noqa: BLE001 - 未安装则用标准库
    _HASHER = None
    HASH_ALGO = "scrypt"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    """``password`` → 可入库的哈希串（自描述算法）。"""
    raw = (password or "").encode("utf-8")
    if _HASHER is not None:
        return _HASHER.hash(raw)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(raw, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """常量时间校验；任何格式异常都返回 False（不抛）。"""
    raw = (password or "").encode("utf-8")
    encoded = str(encoded or "")
    if not encoded:
        return False
    if encoded.startswith("scrypt$"):
        try:
            _, n, r, p, salt_hex, digest_hex = encoded.split("$")
            digest = hashlib.scrypt(
                raw,
                salt=bytes.fromhex(salt_hex),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(bytes.fromhex(digest_hex)),
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except Exception:  # noqa: BLE001
            return False
    if _HASHER is not None:
        try:
            return bool(_HASHER.verify(encoded, raw))
        except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: B014
            return False
    return False


def password_algo() -> str:
    return HASH_ALGO


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _iso_after(minutes: int = 0, days: int = 0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(minutes=minutes, days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def new_csrf_token() -> str:
    """给 Web 双提交 Cookie 用的一次性 CSRF 串。"""
    return _new_token(18)


def normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


def validate_email(email: Any) -> str:
    value = normalize_email(email)
    if not _EMAIL_RE.match(value):
        raise ValueError("邮箱格式不合法")
    return value


def validate_password(password: Any) -> str:
    value = str(password or "")
    if len(value) < _MIN_PASSWORD:
        raise ValueError(f"密码至少 {_MIN_PASSWORD} 位")
    if len(value) > _MAX_PASSWORD:
        raise ValueError("密码过长")
    if value.strip() != value:
        raise ValueError("密码首尾不能有空白字符")
    return value


def invite_required() -> bool:
    return str(os.getenv("SE_INVITE_REQUIRED", "1")).strip().lower() not in ("0", "false", "no")


def mail_dev_mode() -> bool:
    """开发/内测联调：把邮箱令牌直接放在响应里，方便自动化测试。"""
    return str(os.getenv("SE_MAIL_DEV", "0")).strip().lower() in ("1", "true", "yes")


def age_gate_enforced() -> bool:
    """青少年门禁开关（默认关；分龄阶段由运营开启 ``SE_AGE_GATE=1``）。"""
    return str(os.getenv("SE_AGE_GATE", "0")).strip().lower() in ("1", "true", "yes")


DDL = """
CREATE TABLE IF NOT EXISTS v3_accounts (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    password_updated_at TEXT NOT NULL DEFAULT '',
    email_verified_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_invite_codes (
    code TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL DEFAULT '',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_email_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    device_key TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    revoked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_v3_sessions_user ON v3_sessions(user_id, revoked_at);
CREATE TABLE IF NOT EXISTS v3_age_verifications (
    user_id TEXT PRIMARY KEY,
    age_group TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'pending',
    method TEXT NOT NULL DEFAULT '',
    guardian_user_id TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v3_age_audit (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v3_age_audit_user ON v3_age_audit(user_id, created_at DESC);
"""


def ensure_accounts_schema(db: Any) -> None:
    with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层同一把锁
        db.conn.executescript(DDL)
    try:
        setattr(db, "_accounts_schema_ready", True)
    except Exception:  # noqa: BLE001 - 只做惰性优化，不影响正确性
        pass


def _read_one(db: Any, sql: str, params: tuple) -> Any:
    """读一行；老库/精简装配没建表时自动补一次 DDL 再读（幂等）。"""
    try:
        with db._lock:  # noqa: SLF001
            return db.conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        ensure_accounts_schema(db)
        with db._lock:  # noqa: SLF001
            return db.conn.execute(sql, params).fetchone()


def _read_all(db: Any, sql: str, params: tuple) -> list[Any]:
    try:
        with db._lock:  # noqa: SLF001
            return db.conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        ensure_accounts_schema(db)
        with db._lock:  # noqa: SLF001
            return db.conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- #
# 账号
# --------------------------------------------------------------------------- #
def account_for_user(db: Any, user_id: str) -> dict | None:
    row = _read_one(db, "SELECT * FROM v3_accounts WHERE user_id=?", (user_id,))
    return dict(row) if row is not None else None


def account_for_email(db: Any, email: str) -> dict | None:
    row = _read_one(
        db, "SELECT * FROM v3_accounts WHERE email=?", (normalize_email(email),)
    )
    return dict(row) if row is not None else None


def has_account(db: Any, user_id: str) -> bool:
    return account_for_user(db, user_id) is not None


def _insert_account(db: Any, user_id: str, email: str, password_hash: str) -> dict:
    now = _now()
    with db._lock, db.conn:  # noqa: SLF001
        try:
            db.conn.execute(
                "INSERT INTO v3_accounts (user_id, email, password_hash, password_updated_at,"
                " email_verified_at, status, created_at, updated_at) VALUES (?,?,?,'','','active',?,?)",
                (user_id, email, password_hash, now, now),
            )
        except sqlite3.IntegrityError as exc:
            if "v3_accounts.email" in str(exc) or "UNIQUE" in str(exc).upper():
                raise ValueError("邮箱已被注册") from None
            raise
    return account_for_user(db, user_id) or {}


def _ensure_user_row(db: Any, user_id: str, display_name: str = "", device_key: str = "") -> None:
    """账号体系复用 ``v3_users`` 行（好友/动态的归属键），不存在则补一行。"""
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute("SELECT id FROM v3_users WHERE id=?", (user_id,)).fetchone()
        if row is not None:
            if display_name:
                db.conn.execute(
                    "UPDATE v3_users SET display_name=? WHERE id=? AND display_name=''",
                    (display_name[:40], user_id),
                )
            return
        # 账号用户不依赖设备 token 登录，token_hash 置空；设备身份路径会被拒绝
        db.conn.execute(
            "INSERT INTO v3_users (id, device_key, token_hash, display_name, created_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, device_key or None, "", display_name[:40], _now(), _now()),
        )


def _user_for_device_key(db: Any, device_key: str) -> dict | None:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT id, display_name, token_hash FROM v3_users WHERE device_key=?",
            (str(device_key or "").strip(),),
        ).fetchone()
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# 邀请码
# --------------------------------------------------------------------------- #
def create_invite_code(
    db: Any,
    code: str = "",
    *,
    note: str = "",
    max_uses: int = 1,
    expires_at: str = "",
) -> str:
    value = str(code or "").strip() or _new_token(9).replace("-", "").replace("_", "")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_invite_codes (code, note, max_uses, used_count, expires_at, disabled, created_at)"
            " VALUES (?,?,?,0,?,0,?)",
            (value, note[:200], max(1, int(max_uses)), expires_at, _now()),
        )
    return value


def consume_invite_code(db: Any, code: str) -> bool:
    """原子占用一次邀请名额；无效/过期/用尽返回 False。"""
    value = str(code or "").strip()
    if not value:
        return False
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT max_uses, used_count, expires_at, disabled FROM v3_invite_codes WHERE code=?",
            (value,),
        ).fetchone()
        if row is None or int(row["disabled"] or 0):
            return False
        expires = str(row["expires_at"] or "")
        if expires and expires <= _now():
            return False
        if int(row["used_count"] or 0) >= int(row["max_uses"] or 0):
            return False
        db.conn.execute(
            "UPDATE v3_invite_codes SET used_count=used_count+1 WHERE code=?", (value,)
        )
    return True


def list_invite_codes(db: Any, *, limit: int = 200) -> list[dict]:
    rows = _read_all(
        db,
        "SELECT code, note, max_uses, used_count, expires_at, disabled, created_at"
        " FROM v3_invite_codes ORDER BY created_at DESC LIMIT ?",
        (max(1, min(int(limit), 1000)),),
    )
    return [
        {
            "code": str(r["code"]),
            "note": str(r["note"] or ""),
            "max_uses": int(r["max_uses"] or 0),
            "used_count": int(r["used_count"] or 0),
            "remaining": max(0, int(r["max_uses"] or 0) - int(r["used_count"] or 0)),
            "expires_at": str(r["expires_at"] or ""),
            "disabled": bool(int(r["disabled"] or 0)),
            "created_at": str(r["created_at"] or ""),
        }
        for r in rows
    ]


def disable_invite_code(db: Any, code: str) -> bool:
    with db._lock, db.conn:  # noqa: SLF001
        cur = db.conn.execute(
            "UPDATE v3_invite_codes SET disabled=1 WHERE code=?", (str(code or "").strip(),)
        )
        return bool(cur.rowcount)


def search_accounts(db: Any, query: str = "", *, limit: int = 50) -> list[dict]:
    """运营查询账号（邮箱/用户 id 前缀），不返回密码哈希。"""
    like = f"%{str(query or '').strip()}%"
    rows = _read_all(
        db,
        "SELECT a.user_id, a.email, a.email_verified_at, a.status, a.created_at,"
        " u.display_name FROM v3_accounts a LEFT JOIN v3_users u ON u.id=a.user_id"
        " WHERE a.email LIKE ? OR a.user_id LIKE ? ORDER BY a.created_at DESC LIMIT ?",
        (like, like, max(1, min(int(limit), 200))),
    )
    return [
        {
            "user_id": str(r["user_id"]),
            "email": str(r["email"] or ""),
            "email_verified": bool(str(r["email_verified_at"] or "")),
            "status": str(r["status"] or ""),
            "display_name": str(r["display_name"] or ""),
            "created_at": str(r["created_at"] or ""),
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def create_session(
    db: Any, user_id: str, *, device_key: str = "", platform: str = "", ttl_days: int = _SESSION_TTL_DAYS
) -> dict:
    sid = "s_" + secrets.token_hex(12)
    token = _new_token(32)
    now = _now()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_sessions (id, user_id, token_hash, device_key, platform,"
            " created_at, last_seen_at, expires_at, revoked_at) VALUES (?,?,?,?,?,?,?,?,'')",
            (sid, user_id, _hash_token(token), device_key[:128], platform[:32], now, now,
             _iso_after(days=max(1, int(ttl_days)))),
        )
    return {"session_id": sid, "session_token": token, "expires_at": _iso_after(days=ttl_days)}


def authenticate_session(db: Any, token: str) -> dict | None:
    """校验会话 token，返回 ``{"user_id", "session_id"}``；失效/撤销/过期返回 None。"""
    raw = str(token or "").strip()
    if not raw:
        return None
    row = _read_one(
        db,
        "SELECT s.id, s.user_id, s.expires_at, s.revoked_at, a.status AS account_status"
        " FROM v3_sessions s LEFT JOIN v3_accounts a ON a.user_id=s.user_id"
        " WHERE s.token_hash=?",
        (_hash_token(raw),),
    )
    if row is None or str(row["revoked_at"] or ""):
        return None
    account_status = str(row["account_status"] or "active")
    if account_status != "active":
        return None
    expires = str(row["expires_at"] or "")
    if expires and expires <= _now():
        return None
    return {"user_id": str(row["user_id"]), "session_id": str(row["id"])}


def touch_session(db: Any, session_id: str) -> None:
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE v3_sessions SET last_seen_at=? WHERE id=?"
                " AND (last_seen_at='' OR last_seen_at<=?)",
                (_now(), session_id, _iso_after(minutes=-1)),
            )
    except Exception:  # noqa: BLE001 - 活跃时间只用于展示
        pass


def list_sessions(db: Any, user_id: str) -> list[dict]:
    rows = _read_all(
        db,
        "SELECT id, device_key, platform, created_at, last_seen_at, expires_at, revoked_at"
        " FROM v3_sessions WHERE user_id=? ORDER BY created_at DESC",
        (user_id,),
    )
    return [
        {
            "session_id": str(r["id"]),
            "device_key_tail": str(r["device_key"] or "")[-6:],
            "platform": str(r["platform"] or ""),
            "created_at": str(r["created_at"] or ""),
            "last_seen_at": str(r["last_seen_at"] or ""),
            "expires_at": str(r["expires_at"] or ""),
            "revoked": bool(str(r["revoked_at"] or "")),
        }
        for r in rows
    ]


def revoke_session(db: Any, user_id: str, session_id: str) -> bool:
    with db._lock, db.conn:  # noqa: SLF001
        cur = db.conn.execute(
            "UPDATE v3_sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at=''",
            (_now(), session_id, user_id),
        )
        changed = cur.rowcount
    return bool(changed)


def revoke_user_sessions(db: Any, user_id: str, *, except_session_id: str = "") -> int:
    with db._lock, db.conn:  # noqa: SLF001
        if except_session_id:
            cur = db.conn.execute(
                "UPDATE v3_sessions SET revoked_at=? WHERE user_id=? AND revoked_at='' AND id<>?",
                (_now(), user_id, except_session_id),
            )
        else:
            cur = db.conn.execute(
                "UPDATE v3_sessions SET revoked_at=? WHERE user_id=? AND revoked_at=''",
                (_now(), user_id),
            )
        return int(cur.rowcount or 0)


def count_active_sessions(db: Any, user_id: str) -> int:
    row = _read_one(
        db,
        "SELECT COUNT(*) AS n FROM v3_sessions WHERE user_id=? AND revoked_at='' AND expires_at>?",
        (user_id, _now()),
    )
    return int(row["n"] if row is not None else 0)


# --------------------------------------------------------------------------- #
# 邮箱令牌（验证邮箱 / 找回密码）
# --------------------------------------------------------------------------- #
def issue_email_token(db: Any, user_id: str, purpose: str, *, ttl_minutes: int = _TOKEN_TTL_MINUTES) -> str:
    token = _new_token(32)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_email_tokens (id, user_id, purpose, token_hash, expires_at, used_at, created_at)"
            " VALUES (?,?,?,?,?,'',?)",
            ("t_" + secrets.token_hex(8), user_id, purpose, _hash_token(token),
             _iso_after(minutes=max(1, int(ttl_minutes))), _now()),
        )
    return token


def consume_email_token(db: Any, token: str, purpose: str) -> str | None:
    """一次性消费令牌，返回 user_id；无效/过期/已用返回 None。"""
    raw = str(token or "").strip()
    if not raw:
        return None
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT id, user_id, expires_at, used_at FROM v3_email_tokens"
            " WHERE token_hash=? AND purpose=?",
            (_hash_token(raw), purpose),
        ).fetchone()
        if row is None or str(row["used_at"] or ""):
            return None
        if str(row["expires_at"] or "") <= _now():
            return None
        db.conn.execute(
            "UPDATE v3_email_tokens SET used_at=? WHERE id=?", (_now(), row["id"])
        )
    return str(row["user_id"])


def _send_mail(to: str, subject: str, body: str) -> bool:
    """极简邮件出口：默认只记日志（内测人工转发），SMTP 配置齐全时发信。"""
    host = os.getenv("SE_SMTP_HOST", "").strip()
    if not host:
        log.info("邮件(未配置 SMTP，仅记录): to=%s subject=%s body=%s", to, subject, body[:400])
        return False
    try:  # pragma: no cover - 真实发信不在测试覆盖内
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = os.getenv("SE_SMTP_FROM", "noreply@sonder.local")
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        port = int(os.getenv("SE_SMTP_PORT", "465"))
        user = os.getenv("SE_SMTP_USER", "")
        password = os.getenv("SE_SMTP_PASSWORD", "")
        with smtplib.SMTP_SSL(host, port, timeout=10) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("邮件发送失败: %s", exc)
        return False


def _verification_mail(email: str, token: str) -> None:
    _send_mail(email, "Sonder 邮箱验证", f"验证码/链接令牌: {token}\n30 分钟内有效。")


def _reset_mail(email: str, token: str) -> None:
    _send_mail(email, "Sonder 重置密码", f"重置密码令牌: {token}\n30 分钟内有效。")


# --------------------------------------------------------------------------- #
# 注册 / 登录
# --------------------------------------------------------------------------- #
_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_FAIL_WINDOW_S = 900
_FAIL_LIMIT = 5


def _rate_limited(key: str) -> bool:
    now = time.time()
    with _fail_lock:
        bucket = [t for t in _failures.get(key, []) if now - t < _FAIL_WINDOW_S]
        _failures[key] = bucket
        return len(bucket) >= _FAIL_LIMIT


def _record_failure(key: str) -> None:
    with _fail_lock:
        _failures.setdefault(key, []).append(time.time())


def clear_failures(key: str = "") -> None:
    """测试/运维用：清空失败计数（不传 key 清全部）。"""
    with _fail_lock:
        if key:
            _failures.pop(key, None)
        else:
            _failures.clear()


def register_email(
    db: Any,
    email: str,
    password: str,
    *,
    invite_code: str = "",
    display_name: str = "",
    device_key: str = "",
    platform: str = "",
) -> dict:
    """邮箱注册。``device_key`` 命中未绑定的老设备身份时沿用该 user_id（保数据）。"""
    mail = validate_email(email)
    pwd = validate_password(password)
    if account_for_email(db, mail) is not None:
        raise ValueError("邮箱已被注册")
    if invite_required() and not consume_invite_code(db, invite_code):
        raise PermissionError("邀请码无效或已用尽")

    user_id = ""
    legacy = _user_for_device_key(db, device_key) if device_key else None
    if legacy is not None:
        if has_account(db, str(legacy["id"])):
            raise ValueError("该设备已绑定账号，请直接登录")
        user_id = str(legacy["id"])
        display_name = display_name or str(legacy["display_name"] or "")
        _ensure_user_row(db, user_id, display_name, device_key=str(device_key or ""))
    else:
        from app.v3.identity import is_valid_user_id  # noqa: PLC0415 - 避免顶层循环依赖

        user_id = "u_" + secrets.token_hex(12)
        assert is_valid_user_id(user_id)
        _ensure_user_row(db, user_id, display_name, device_key=str(device_key or ""))

    _insert_account(db, user_id, mail, hash_password(pwd))
    token = issue_email_token(db, user_id, "verify_email")
    _verification_mail(mail, token)
    session = create_session(db, user_id, device_key=str(device_key or ""), platform=platform)
    log.info("账号注册: %s email=%s", user_id, mail)
    result = {
        "user_id": user_id,
        "email": mail,
        "display_name": display_name,
        "email_verified": False,
        "created": True,
        **session,
        "password_algo": password_algo(),
    }
    if mail_dev_mode():
        result["dev_verification_token"] = token
    return result


def login(
    db: Any,
    email: str,
    password: str,
    *,
    device_key: str = "",
    platform: str = "",
) -> dict:
    mail = normalize_email(email)
    key = f"login:{mail}"
    if _rate_limited(key):
        raise PermissionError("尝试次数过多，请稍后再试")
    account = account_for_email(db, mail)
    if account is None or not verify_password(password, str(account["password_hash"])):
        _record_failure(key)
        raise PermissionError("邮箱或密码不正确")
    if str(account["status"] or "active") != "active":
        raise PermissionError("账号已被停用")
    clear_failures(key)
    user_id = str(account["user_id"])
    session = create_session(db, user_id, device_key=str(device_key or ""), platform=platform)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute("UPDATE v3_users SET last_seen_at=? WHERE id=?", (_now(), user_id))
    return {
        "user_id": user_id,
        "email": mail,
        "display_name": str((_v3_user(db, user_id) or {}).get("display_name") or ""),
        "email_verified": bool(account["email_verified_at"]),
        "created": False,
        **session,
    }


def _v3_user(db: Any, user_id: str) -> dict | None:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT id, display_name, device_key, created_at FROM v3_users WHERE id=?", (user_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def verify_email(db: Any, token: str) -> str:
    user_id = consume_email_token(db, token, "verify_email")
    if user_id is None:
        raise ValueError("验证令牌无效或已过期")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE v3_accounts SET email_verified_at=?, updated_at=? WHERE user_id=?",
            (_now(), _now(), user_id),
        )
    return user_id


def resend_verification(db: Any, user_id: str) -> str:
    account = account_for_user(db, user_id)
    if account is None:
        raise ValueError("当前身份没有邮箱账号")
    if account["email_verified_at"]:
        raise ValueError("邮箱已验证")
    token = issue_email_token(db, user_id, "verify_email")
    _verification_mail(str(account["email"]), token)
    return token


def request_password_reset(db: Any, email: str) -> str | None:
    """无论邮箱是否存在都返回成功语义；存在则返回（dev 模式下可下发的）令牌。"""
    account = account_for_email(db, email)
    if account is None:
        return None
    token = issue_email_token(db, str(account["user_id"]), "reset_password")
    _reset_mail(str(account["email"]), token)
    return token


def reset_password(db: Any, token: str, new_password: str) -> str:
    """按令牌重置密码，并撤销该账号全部会话（含未知设备）。"""
    pwd = validate_password(new_password)
    user_id = consume_email_token(db, token, "reset_password")
    if user_id is None:
        raise ValueError("重置令牌无效或已过期")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE v3_accounts SET password_hash=?, password_updated_at=?, updated_at=?"
            " WHERE user_id=?",
            (hash_password(pwd), _now(), _now(), user_id),
        )
    revoke_user_sessions(db, user_id)
    log.info("密码已重置并撤销全部会话: %s", user_id)
    return user_id


def change_password(db: Any, user_id: str, old_password: str, new_password: str) -> None:
    account = account_for_user(db, user_id)
    if account is None:
        raise ValueError("当前身份没有邮箱账号")
    if not verify_password(old_password, str(account["password_hash"])):
        raise PermissionError("原密码不正确")
    pwd = validate_password(new_password)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE v3_accounts SET password_hash=?, password_updated_at=?, updated_at=?"
            " WHERE user_id=?",
            (hash_password(pwd), _now(), _now(), user_id),
        )
    revoke_user_sessions(db, user_id)


def bind_legacy_user(
    db: Any,
    user_id: str,
    email: str,
    password: str,
    *,
    invite_code: str = "",
) -> dict:
    """把已存在的设备身份升级成邮箱账号，保留好友/聊天/动态。"""
    if not user_id or user_id == LOCAL_USER_ID:
        raise ValueError("local 身份不能绑定账号")
    if has_account(db, user_id):
        raise ValueError("该身份已绑定账号")
    legacy = _v3_user(db, user_id)
    if legacy is None:
        raise ValueError("身份不存在")
    mail = validate_email(email)
    pwd = validate_password(password)
    if account_for_email(db, mail) is not None:
        raise ValueError("邮箱已被注册，请直接登录")
    if invite_required() and not consume_invite_code(db, invite_code):
        raise PermissionError("邀请码无效或已用尽")
    _insert_account(db, user_id, mail, hash_password(pwd))
    token = issue_email_token(db, user_id, "verify_email")
    _verification_mail(mail, token)
    session = create_session(db, user_id, device_key=str(legacy.get("device_key") or ""))
    result = {
        "user_id": user_id,
        "email": mail,
        "email_verified": False,
        "created": False,
        **session,
    }
    if mail_dev_mode():
        result["dev_verification_token"] = token
    log.info("设备身份已绑定邮箱账号: %s", user_id)
    return result


# --------------------------------------------------------------------------- #
# 分龄与能力
# --------------------------------------------------------------------------- #
VALID_AGE_GROUPS = ("unknown", "adult", "teen")
VALID_AGE_STATUS = ("pending", "verified", "rejected")


def age_verification_for(db: Any, user_id: str) -> dict | None:
    row = _read_one(db, "SELECT * FROM v3_age_verifications WHERE user_id=?", (user_id,))
    return dict(row) if row is not None else None


def set_age_verification(
    db: Any,
    user_id: str,
    *,
    age_group: str,
    status: str = "verified",
    method: str = "ops",
    guardian_user_id: str = "",
    note: str = "",
    actor: str = "ops",
    expires_at: str = "",
) -> dict:
    """记录运营核验结论（不收集证件）。所有变更写审计。"""
    group = str(age_group or "").strip().lower()
    if group not in VALID_AGE_GROUPS:
        raise ValueError("非法年龄组")
    state = str(status or "").strip().lower()
    if state not in VALID_AGE_STATUS:
        raise ValueError("非法核验状态")
    now = _now()
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO v3_age_verifications (user_id, age_group, status, method, guardian_user_id,"
            " verified_at, expires_at, note, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET age_group=excluded.age_group, status=excluded.status,"
            " method=excluded.method, guardian_user_id=excluded.guardian_user_id,"
            " verified_at=excluded.verified_at, expires_at=excluded.expires_at,"
            " note=excluded.note, updated_at=excluded.updated_at",
            (user_id, group, state, str(method)[:40], str(guardian_user_id)[:64],
             now if state == "verified" else "", str(expires_at or ""), str(note)[:400], now),
        )
        db.conn.execute(
            "INSERT INTO v3_age_audit (id, user_id, action, detail, actor, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("a_" + secrets.token_hex(8), user_id, "set_age_verification",
             f'{{"age_group":"{group}","status":"{state}","method":"{str(method)[:40]}"}}',
             str(actor)[:64], now),
        )
    return age_verification_for(db, user_id) or {}


def _features_for(db: Any, user_id: str, account: dict | None, age: dict | None, group: str) -> dict:
    base = {
        "chat": True,
        "calls": True,
        "feed": True,
        "ads": False,
        "intimate_relationship": True,
        "help": True,
        "learning": True,
    }
    if account is None:
        # 设备身份/本地模式：内测联调用，广告永远不投
        return {**base, "ads": False, "age_gate": "legacy"}
    base["calls"] = bool(account.get("status") == "active")
    base["chat"] = bool(account.get("status") == "active")
    base["ads"] = False
    if age_gate_enforced():
        if group == "adult":
            base["ads"] = True
            base["intimate_relationship"] = True
        elif group == "teen":
            base["ads"] = False
            base["intimate_relationship"] = False
        else:  # unknown / pending
            base.update(chat=False, calls=False, feed=False, ads=False,
                        intimate_relationship=False, learning=False)
    else:
        base["ads"] = group == "adult"
        base["intimate_relationship"] = group != "teen"
    return base


def capabilities(db: Any, user_id: str) -> dict:
    """统一账号状态与可用能力；前端据此决定登录态/引导/广告位。"""
    account = account_for_user(db, user_id)
    age = age_verification_for(db, user_id)
    group = str((age or {}).get("age_group") or "unknown")
    verified = bool(age and str(age.get("status") or "") == "verified")
    if group not in VALID_AGE_GROUPS:
        group = "unknown"
    return {
        "user_id": user_id,
        "account": {
            "type": "email" if account else ("legacy" if user_id != LOCAL_USER_ID else "local"),
            "status": str((account or {}).get("status") or "legacy"),
            "email_verified": bool((account or {}).get("email_verified_at")),
            "has_password": account is not None,
        },
        "age": {
            "group": group,
            "verified": verified,
            "guardian_bound": bool((age or {}).get("guardian_user_id")),
            "enforced": age_gate_enforced(),
        },
        "features": _features_for(db, user_id, account, age, group),
        "password_algo": password_algo(),
        "sessions": count_active_sessions(db, user_id) if account else 0,
    }


def require_feature(db: Any, user_id: str, feature: str) -> None:
    """供路由/后台任务统一判定；不满足抛 ``PermissionError``（路由层转 403）。"""
    features = capabilities(db, user_id)["features"]
    if not features.get(feature, False):
        raise PermissionError(f"当前账号不可用能力: {feature}")


def current_account_user() -> str:
    return current_user_id()


__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "HASH_ALGO",
    "SESSION_COOKIE",
    "account_for_email",
    "account_for_user",
    "age_gate_enforced",
    "age_verification_for",
    "authenticate_session",
    "bind_legacy_user",
    "capabilities",
    "change_password",
    "clear_failures",
    "consume_invite_code",
    "count_active_sessions",
    "create_invite_code",
    "create_session",
    "ensure_accounts_schema",
    "hash_password",
    "has_account",
    "invite_required",
    "issue_email_token",
    "list_sessions",
    "login",
    "mail_dev_mode",
    "normalize_email",
    "password_algo",
    "register_email",
    "request_password_reset",
    "require_feature",
    "resend_verification",
    "reset_password",
    "revoke_session",
    "revoke_user_sessions",
    "set_age_verification",
    "touch_session",
    "validate_email",
    "validate_password",
    "verify_email",
    "verify_password",
]
