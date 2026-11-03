"""安装包入口：`python -m app` 等价于 uvicorn app.main:app。"""

from __future__ import annotations

import os

import uvicorn

from app.config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.server.host,
        port=s.server.port,
        reload=bool(os.getenv("DEV_RELOAD")),
        log_level=s.server.log_level.lower(),
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    main()
