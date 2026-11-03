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

VERSION = "0.1.0"

log = get_logger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
APP_DIR = os.path.join(STATIC_DIR, "app")          # Next.js 静态导出产物
STANDALONE = os.path.join(STATIC_DIR, "standalone.html")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.server.log_level, force=True)
    log.info("=" * 68)
    log.info("SpeakEasy 后端启动 v%s", VERSION)
    log.info("配置: 数据目录=%s", settings.data_path)
    log.info("LLM: base_url=%s model=%s configured=%s",
             settings.llm.base_url, settings.llm.model, bool(settings.llm.api_key))
    log.info("STT: backend=%s model=%s device=%s compute=%s",
             settings.stt.backend, settings.stt.model_size, settings.stt.device, settings.stt.compute_type)
    log.info("TTS: backend=%s device=%s", settings.tts.backend, settings.tts.device)
    log.info("GPU: %s", {k: v for k, v in gpu_info().items() if k != "error"})

    engines = build_engines(settings)
    app.state.engines = engines
    app.state.version = VERSION

    # SpeakEasy 2.0：SQLite + 记忆/社交/朋友圈服务（惰性加载，不阻塞启动）
    from app.v2.services import build_services  # noqa: PLC0415

    app.state.v2 = build_services(engines, settings.data_path)

    # 会话存储（WS 连接共享）
    from app.session import SessionStore

    app.state.session_store = SessionStore(ttl_s=float(os.getenv("SESSION_TTL_S", "3600")))

    # 参考音频体检（只提示，不阻断）
    try:
        problems = engines.ref_audio.validate()
        if problems:
            log.warning("参考音频检查发现 %d 个问题（可用 scripts/fetch_ref_audio.py 或录音补全）:", len(problems))
            for p in problems[:8]:
                log.warning("  - %s", p)
    except Exception as exc:  # pragma: no cover
        log.warning("参考音频校验失败: %s", exc)

    # 后台预热，不阻塞启动
    warm_task: asyncio.Task | None = None
    if settings.tts.prewarm or settings.stt.backend != "mock":
        warm_task = asyncio.create_task(engines.prewarm("all"), name="prewarm")
        app.state.warm_task = warm_task

    # 音频保留策略：总量上限 2 GB，超出按最旧优先删（转写文本与记忆保留）
    from app.v2.audio_retention import MAX_BYTES as _AUDIO_CAP  # noqa: PLC0415
    from app.v2.audio_retention import retention_loop  # noqa: PLC0415

    retention_task = asyncio.create_task(
        retention_loop(
            db_path=os.path.join(str(settings.data_path), "speakeasy_v2.db"),
            audio_dir=os.path.join(str(settings.data_path), "audio"),
            max_bytes=_AUDIO_CAP,
        ),
        name="audio-retention",
    )

    log.info("就绪: http://%s:%s  (前端: / )", settings.server.host, settings.server.port)
    log.info("=" * 68)
    try:
        yield
    finally:
        log.info("关闭中…")
        if warm_task and not warm_task.done():
            warm_task.cancel()
            try:
                await warm_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        if not retention_task.done():
            retention_task.cancel()
            try:
                await retention_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        v2 = getattr(app.state, "v2", None)
        if v2 is not None:
            try:
                await v2.aclose()
            except Exception as exc:  # noqa: BLE001
                log.debug("v2 关闭失败: %s", exc)
        await engines.aclose()
        log.info("已关闭")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.server.log_level)

    app = FastAPI(
        title="SpeakEasy · 英语口语陪练",
        version=VERSION,
        description="本地语音栈（Silero-VAD + faster-whisper + CosyVoice2/F5-TTS）+ 云端 LLM 的实时英语口语对练。",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.server.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(http_api.router)
    app.include_router(llm_config_api.router)
    app.include_router(voice_api.router)
    app.include_router(market_api.router)
    app.include_router(translate_api.router)
    app.include_router(conversations_api.router)
    app.include_router(v2_router)

    # 对话持久化存储
    from app.conversations import ConversationStore  # noqa: PLC0415

    app.state.conversation_store = ConversationStore(settings.data_path)
    log.info("对话持久化: %s", app.state.conversation_store.path)
    app.include_router(ws.router)

    _mount_static(app, settings)
    return app


def _mount_static(app: FastAPI, settings) -> None:
    """挂载前端：优先 Next 构建产物，其次 standalone.html。"""

    if not settings.server.serve_frontend:
        log.info("serve_frontend=False，跳过静态挂载")
        return

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
