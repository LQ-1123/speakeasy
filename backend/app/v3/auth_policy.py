"""Runtime authentication mode, shared by HTTP and background entry points."""

import os


def email_code_auth_enabled() -> bool:
    return os.getenv("SE_EMAIL_CODE_AUTH", "1").strip().lower() not in {"0", "false", "no"}


def require_legacy_auth() -> None:
    if email_code_auth_enabled():
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail={"code": "EMAIL_CODE_REQUIRED", "message": "请使用邮箱验证码登录"})
