"""FastAPI 应用入口。

启动顺序：
1. 读配置、配置日志
2. 装配引擎（不加载权重）
3. 后台任务预热 STT/TTS（不阻塞启动）
4. 挂载静态前端 + 路由

即便没有 GPU / 没有模型 / 没有 LLM key，本应用也必须能启动并跑通文字链路。
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.engines import build_engines, gpu_info
from app.logging_utils import configure_logging, get_logger
from app.routers import http_api, ws
from app.routers import llm_config_api
from app.v2.router import router as v2_router
from app.routers import voice_api
from app.routers import market_api
from app.routers import conversations_api
from app.routers import translate_api
from app.v3.router import router as v3_router
from app.v3.calls import router as calls_router
from app.v3.client_api import router as client_router, ensure_client_schema
from app.v3.schema import ensure_schema
from app.v3.service import V3Service

VERSION = "0.1.0"

log = get_logger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
APP_DIR = os.path.join(STATIC_DIR, "app")          # Next.js 静态导出产物
STANDALONE = os.path.join(STATIC_DIR, "standalone.html")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Strict voice identity is the production default; offline fixtures may opt out.
    os.environ.setdefault("SE_VOICE_STRICT", "1")
    settings = get_settings()
    configure_logging(settings.server.log_level, force=True)
    log.info("SpeakEasy backend starting v%s data=%s", VERSION, settings.data_path)
    engines = build_engines(settings)
    app.state.engines = engines
    app.state.version = VERSION

    from app.v2.services import build_services
    from app.v3 import client_events
    from app.v3 import calls
    from app.v3.audio_cache import PersonaAudioCache
    from app.voice.catalog import repair_persona_voices
    from app.jobs import DurableJobQueue
    from app.maintenance import maintenance_loop
    from app.session import SessionStore

    app.state.v2 = build_services(engines, settings.data_path)
    db = app.state.v2.db
    ensure_schema(db, seed=False)
    ensure_client_schema(db)
    # 账号体系（邮箱凭据/邀请码/邮箱令牌/可撤销设备会话/年龄核验）
    from app.v3.accounts import ensure_accounts_schema  # noqa: PLC0415
    from app.v3.media_access import ensure_media_schema  # noqa: PLC0415
    from app.v3.privacy import ensure_privacy_schema  # noqa: PLC0415
    from app.v3.teen import ensure_teen_schema  # noqa: PLC0415

    ensure_accounts_schema(db)
    ensure_privacy_schema(db)
    # 媒体归属（私聊音频/图片 owner 校验与短时令牌）
    ensure_media_schema(db)
    # 青少年模式（监护关系/时长/宵禁）
    ensure_teen_schema(db)
    # 个性化推荐流（用户专属快照 + 已看记录）
    from app.v3.recommend import ensure_recommend_schema  # noqa: PLC0415

    ensure_recommend_schema(db)
    # 商业合作（品牌/活动/素材/投放/频控/留痕）
    from app.v3.ads import ensure_ads_schema  # noqa: PLC0415

    ensure_ads_schema(db)
    # 内容举报（通用举报表；广告另有投放事件）
    from app.v3.reports import ensure_reports_schema  # noqa: PLC0415

    ensure_reports_schema(db)
    # 标签统一（自由文本 → 稳定 ID；幂等迁移历史资料/人物/动态）
    from app.v3.taxonomy import migrate_tags  # noqa: PLC0415

    migrate_tags(db)
    repair_persona_voices(db)
    app.state.v3 = V3Service(db, engines, str(settings.data_path))
    app.state.v3_audio = PersonaAudioCache(
        db, app.state.v2.voice, os.path.join(str(settings.data_path), "v3_audio"))
    app.state.session_store = SessionStore(ttl_s=float(os.getenv("SESSION_TTL_S", "3600")))
    app.state.background_jobs = DurableJobQueue(db.conn, lock=db._lock)

    # No provider or world generation is awaited before serving requests.
    tasks = [
        asyncio.create_task(maintenance_loop(app, settings, engines, app.state.background_jobs),
                            name="durable-maintenance"),
        asyncio.create_task(client_events.event_worker(db, app.state.v3), name="client-events"),
        asyncio.create_task(calls.cleanup_sessions(app), name="call-cleanup"),
    ]
    app.state.maintenance_task = tasks[0]
    app.state.client_events_task = tasks[1]
    if settings.tts.prewarm or settings.stt.backend != "mock":
        task = asyncio.create_task(engines.prewarm("all"), name="prewarm")
        app.state.warm_task = task
        tasks.append(task)
    try:
        yield
    finally:
        # Every DB-using worker stops before V2Services closes SQLite.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for call_id, runtime in list(calls.RUNTIMES.items()):
            await calls.finish_call(app, call_id, runtime.user_id)
        cache = app.state.v3_audio
        cache.stop()
        if cache._worker is not None:
            await asyncio.gather(cache._worker, return_exceptions=True)
        await app.state.v2.aclose()
        await engines.aclose()
        log.info("SpeakEasy backend stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.server.log_level)

    app = FastAPI(
        title="SpeakEasy · 英语口语陪练",
        version=VERSION,
        description="本地语音栈（Silero-VAD + faster-whisper + CosyVoice2/F5-TTS）+ 云端 LLM 的实时英语口语对练。",
        lifespan=lifespan,
    )

    # 前后端分离：CORS 白名单可用环境变量覆盖（逗号分隔）
    import os as _os

    _env_origins = [
        o.strip() for o in _os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(dict.fromkeys([*(_env_origins or settings.server.cors_origins), "https://localhost"])),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    app.include_router(http_api.router)
    app.include_router(llm_config_api.router)
    app.include_router(voice_api.router)
    app.include_router(market_api.router)
    app.include_router(translate_api.router)
    app.include_router(conversations_api.router)
    app.include_router(v2_router)
    app.include_router(v3_router)
    from app.v3.accounts_router import router as accounts_router  # noqa: PLC0415
    from app.v3.ads_router import router as ads_router  # noqa: PLC0415
    from app.v3.media_access_router import router as media_access_router  # noqa: PLC0415
    from app.v3.reports_router import router as reports_router  # noqa: PLC0415
    from app.v3.teen_router import router as teen_router  # noqa: PLC0415

    app.include_router(accounts_router)
    app.include_router(ads_router)
    app.include_router(media_access_router)
    app.include_router(reports_router)
    app.include_router(teen_router)
    app.include_router(calls_router)
    app.include_router(client_router)

    # 管理端（控制面）：Sonder 运营控制台。仅管理身份可用，与客户端完全解耦。
    from app.admin import api_public_router as admin_public_router  # noqa: PLC0415
    from app.admin import page_router as admin_page_router  # noqa: PLC0415
    from app.admin import router as admin_router  # noqa: PLC0415

    from app.app_release import router as app_release_router  # noqa: PLC0415

    app.include_router(app_release_router)
    app.include_router(admin_router)
    # 不鉴权的管理 API（会话查询 / 退出）：必须能在未登录时调通，
    # 所以不能挂在带 require_admin 依赖的 admin_router 下。
    app.include_router(admin_public_router)
    app.include_router(admin_page_router)

    # 对话持久化存储
    from app.conversations import ConversationStore  # noqa: PLC0415

    app.state.conversation_store = ConversationStore(settings.data_path)
    log.info("对话持久化: %s", app.state.conversation_store.path)
    app.include_router(ws.router)

    _mount_static(app, settings)
    return app


# Windows 的 mimetypes 缺 webp，补上（否则 /media/*.webp 返回 octet-stream）
mimetypes.add_type("image/webp", ".webp")


def _mount_static(app: FastAPI, settings) -> None:
    """挂载前端：优先 Next 构建产物，其次 standalone.html。"""

    if not settings.server.serve_frontend:
        log.info("serve_frontend=False，跳过静态挂载")
        return

    # 开放图库图片：/media/*（必须在 "/" 之前挂载，否则被前端静态路由吞掉）
    media_dir = os.path.join(str(settings.data_path), "media")
    os.makedirs(media_dir, exist_ok=True)
    app.mount("/media", StaticFiles(directory=media_dir), name="media")

    # 用户上传图片：/uploads/*
    # 多用户隔离：按 media_assets 归属校验（owner 本人或有效 ?t= 令牌），
    # 未登记的历史文件保持可读并标记 X-Legacy-Asset: 1。
    uploads_dir = os.path.join(str(settings.data_path), "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    from app.v3.uploads_guard import ProtectedUploads  # noqa: PLC0415

    app.mount(
        "/uploads",
        ProtectedUploads(directory=uploads_dir, kind="post_image"),
        name="uploads",
    )

    if os.path.isdir(APP_DIR) and os.path.exists(os.path.join(APP_DIR, "index.html")):
        # Next static export 需要把 /_next/* 正确映射；直接整目录挂到 / 会被 /api 抢走
        app.mount("/", StaticFiles(directory=APP_DIR, html=True), name="frontend")
        log.info("静态前端: Next 构建产物 %s", APP_DIR)

        @app.exception_handler(404)
        async def _spa_fallback(request: Request, exc):  # pragma: no cover
            # 非 API 路径的 404 回落到 index.html（单页应用路由）
            path = request.url.path
            if path.startswith(("/api", "/ws")):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            idx = os.path.join(APP_DIR, "index.html")
            if os.path.exists(idx):
                return FileResponse(idx)
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        return

    if os.path.exists(STANDALONE):
        log.info("静态前端: standalone.html（尚未构建 Next 前端）")

        @app.get("/", include_in_schema=False)
        async def _standalone():
            return FileResponse(STANDALONE, media_type="text/html; charset=utf-8")

    else:
        log.warning("未找到前端产物，仅提供 API。请构建 frontend 或添加 static/standalone.html")

    # 两者都挂以便调试资源
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

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
