"""V2 media elements carry scoped tickets instead of account headers."""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.v3.auth_policy import email_code_auth_enabled
from app.v3.identity import set_current_user
from app.v3.media_access import owner_of_filename, verify_media_token
from app.v3.security import bind_current_user


async def bind_v2_user(request: Request) -> str:
    path = request.url.path
    if email_code_auth_enabled() and request.method == "GET" and (
        path.startswith("/api/v2/audio/") or path.startswith("/api/v2/media/")
    ) and request.query_params.get("t") and not any(request.headers.get(name) for name in (
        "x-session-token", "authorization", "x-user-id", "x-user-token"
    )):
        db = getattr(getattr(request.app.state, "v2", None), "db", None)
        ticket = verify_media_token(db, request.query_params["t"]) if db else None
        expected_kind = "audio" if path.startswith("/api/v2/audio/") else None
        if ticket and ticket[2] == request.path_params.get("filename") and (not expected_kind or ticket[1] == expected_kind):
            owner = owner_of_filename(db, ticket[2], kinds=(ticket[1],))
            if owner and owner[0] == ticket[0]:
                set_current_user(ticket[0])
                return ticket[0]
        raise HTTPException(status_code=403, detail="媒体令牌无效或已过期")
    return await bind_current_user(request)
