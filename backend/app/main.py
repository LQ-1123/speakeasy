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
from app.v3.schema import ensure_schema
from app.v3.service import V3Service

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

    # SpeakEasy 3.0：Persona / Post / Friendship / SocialEvent（同库，自动迁移）
    try:
        from app.v3.world import ensure_world  # noqa: PLC0415

        ensure_schema(app.state.v2.db, seed=False)
        # 世界生成：一次捏出「人物 + 信息流」；之后偶遇只是从这个世界里挑人
        world = await ensure_world(app.state.v2.db, engines.llm)
        app.state.v3 = V3Service(app.state.v2.db, engines, str(settings.data_path))
        log.info("v3 服务就绪：world=%s social=%s", world, app.state.v3.config.public())

        # 词库（捏人原料）：启动时应用一次，后台保存后也会自动重新应用
        try:
            from app.v3 import lexicon  # noqa: PLC0415

            applied = lexicon.refresh()
            log.info("词库已加载：%s", applied)
        except Exception as exc:  # noqa: BLE001 - 词库有问题不能影响启动
            log.warning("词库加载失败（用代码默认值）: %s", exc)

        # 先分配每人的专属音色，再预热朗读音频（否则预合成会统一用全局参考音色）
        try:
            await app.state.v3.assign_fish_voices()
        except Exception as exc:  # noqa: BLE001 - 失败不影响启动
            log.debug("v3 Fish 音色分配失败: %s", exc)

        # 朗读毫秒级：后台把信息流第一条音频预先合成并落盘，点击直接读文件
        from app.v3.audio_cache import PersonaAudioCache  # noqa: PLC0415

        app.state.v3_audio = PersonaAudioCache(
            app.state.v2.db,
            app.state.v2.voice,
            os.path.join(str(settings.data_path), "v3_audio"),
        )
        app.state.v3_audio.start()
        queued = app.state.v3_audio.warm(app.state.v3)
        log.info("v3 朗读预热已排队 %d 条（后台合成，不阻塞启动）", queued)

        # 每日世界：启动补今天内容，之后每天 02:00 生成；顺带 TTL 清理
        from app.v3.daily_world import daily_loop  # noqa: PLC0415

        app.state.daily_task = asyncio.create_task(
            daily_loop(
                app.state.v2.db,
                engines.llm,
                media_dir=os.path.join(str(settings.data_path), "media"),
            ),
            name="v3-daily-world",
        )

        # 存量人物补 Fish 专属音色（按性别+语言从音色库挑，幂等，后台跑不阻塞）
        async def _assign_voices() -> None:
            try:
                count = await app.state.v3.assign_fish_voices()
                if count:
                    log.info("v3 Fish 音色分配完成：%d 个", count)
            except Exception as exc:  # noqa: BLE001 - 失败不影响主流程
                log.debug("v3 Fish 音色分配失败: %s", exc)

        app.state.v3_voice_task = asyncio.create_task(
            _assign_voices(), name="v3-fish-voices"
        )

        # Fish 语音冷启动探测：成功才把 TTS 切到 Fish（health 里 attempt_ok=true）
        async def _probe_fish() -> None:
            from app.voice import voice_registry  # noqa: PLC0415

            for attempt, delay in enumerate((0.0, 4.0, 12.0)):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    if await voice_registry.probe():
                        log.info("Fish 语音就绪（启动探测成功，第 %d 次）", attempt + 1)
                        return
                except Exception as exc:  # noqa: BLE001 - 探测失败不影响启动
                    log.debug("Fish 启动探测异常: %s", exc)
            log.warning("Fish 启动探测未成功，暂用 Edge（冷却后首个请求会自动重试）")

        app.state.fish_probe_task = asyncio.create_task(
            _probe_fish(), name="fish-probe"
        )

        # ---- Persona 生活系统（Speech DNA / 生活事件 / 事件驱动动态）----
        # 全部后台化、幂等、可失败降级：网页必须在这些任务跑完前就能打开。
        from app.v3.event_posts import run_posts_cycle  # noqa: PLC0415
        from app.v3.life_events import ensure_relationships, life_loop  # noqa: PLC0415
        from app.v3.speech_dna import ensure_persona_dna  # noqa: PLC0415

        async def _warm_persona_life() -> None:
            """补齐 Speech DNA + 示例对话 + 人物间关系（幂等，只补缺的）。

            成本控制：常驻人物走 LLM 高质量示例（一次性，每人 1 次调用）；
            每日游客人物只用确定性示例库（0 次调用）——它们 24h 后会回收。
            """
            db = app.state.v2.db
            permanent: list[dict] = []
            guests: list[dict] = []
            try:
                with db._lock:  # noqa: SLF001 - 只读
                    rows = [dict(r) for r in db.conn.execute("SELECT * FROM personas").fetchall()]
                for row in rows:
                    if str(row.get("lifecycle") or "permanent") == "guest":
                        guests.append(row)
                    else:
                        permanent.append(row)
            except Exception as exc:  # noqa: BLE001
                log.warning("读取人物列表失败（跳过表达层预热）: %s", exc)
            try:
                stats = await ensure_persona_dna(db, engines.llm, permanent)
                log.info("Persona 表达层就绪（常驻）: %s", stats)
            except Exception as exc:  # noqa: BLE001 - 缺表达层仍可聊天（会退化）
                log.warning("Persona Speech DNA/示例生成失败（降级）: %s", exc)
            if guests:
                try:
                    stats = await ensure_persona_dna(db, None, guests, use_llm=False)
                    log.info("Persona 表达层就绪（游客，0 调用）: %s", stats)
                except Exception as exc:  # noqa: BLE001
                    log.debug("游客表达层补齐失败（忽略）: %s", exc)
            try:
                count = ensure_relationships(db)
                log.info("Persona 人际关系就绪：%d 条", count)
            except Exception as exc:  # noqa: BLE001
                log.warning("Persona 关系生成失败（忽略）: %s", exc)

        app.state.persona_life_task = asyncio.create_task(
            _warm_persona_life(), name="v3-persona-dna"
        )

        # 生活事件：规则作息 + 叙事事件 + 状态推进（每天一批，幂等）
        # tick 默认 300s：状态推进 + 关系/情绪联动的频率；本地验收可调小。
        # 世界节奏优先读词库（后台可调），环境变量仍可覆盖；tick 改动重启后生效
        try:
            from app.v3 import lexicon as _lexicon  # noqa: PLC0415

            _rhythm = (_lexicon.load("rhythm") or {}).get("params") or {}
        except Exception:  # noqa: BLE001
            _rhythm = {}

        life_tick = max(
            5.0,
            float(_rhythm.get("life_tick_s") or os.getenv("SE_LIFE_TICK_S", "300") or 300),
        )
        app.state.life_task = asyncio.create_task(
            life_loop(app.state.v2.db, engines.llm, tick_s=life_tick), name="v3-life-events"
        )

        # 事件 → 动态：只处理最近完成的事件；刷新信息流永远不会触发这里
        posts_tick = max(
            60.0,
            float(_rhythm.get("posts_tick_s") or os.getenv("SE_POSTS_TICK_S", "240") or 240),
        )
        posts_max = max(
            1,
            int(float(_rhythm.get("posts_max_events") or os.getenv("SE_POSTS_MAX_EVENTS", "4") or 4)),
        )
        posts_window = max(
            1.0,
            float(_rhythm.get("posts_since_hours") or os.getenv("SE_POSTS_SINCE_HOURS", "6") or 6),
        )

        async def _event_posts_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(posts_tick)
                    stats = await run_posts_cycle(
                        app.state.v2.db,
                        engines.llm,
                        max_events=posts_max,
                        since_hours=posts_window,
                    )
                    if stats.get("posts"):
                        log.info("事件驱动动态：%s", stats)
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # noqa: BLE001 - 后台失败不拖垮服务
                    log.warning("事件动态批处理失败（下轮重试）: %s", exc)

        app.state.posts_task = asyncio.create_task(
            _event_posts_loop(), name="v3-event-posts"
        )
    except Exception as exc:  # noqa: BLE001 - v3 失败不阻断既有功能
        log.exception("v3 服务初始化失败（发现页将不可用）: %s", exc)
        app.state.v3 = None

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

    # 图库保留策略：过期未引用 → 缺文件行 → 超配额（最旧未引用优先），
    # 被动态引用的图永不删除；默认 200MB / 800 张 / 45 天，可环境变量覆盖。
    from app.admin import media_admin as _media_admin  # noqa: PLC0415

    app.state.media_task = asyncio.create_task(
        _media_admin.janitor_loop(
            app.state.v2.db,
            interval_s=float(os.getenv("SE_MEDIA_JANITOR_INTERVAL_S", "3600") or 3600),
        ),
        name="media-janitor",
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
        v3_voice_task = getattr(app.state, "v3_voice_task", None)
        if v3_voice_task is not None and not v3_voice_task.done():
            v3_voice_task.cancel()
        daily_task = getattr(app.state, "daily_task", None)
        if daily_task is not None and not daily_task.done():
            daily_task.cancel()
            try:
                await daily_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        # Persona 生活系统的后台任务
        for name in ("persona_life_task", "life_task", "posts_task", "media_task"):
            task = getattr(app.state, name, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: B014
                    pass
        audio_cache = getattr(app.state, "v3_audio", None)
        if audio_cache is not None:
            audio_cache.stop()
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

    # 前后端分离：CORS 白名单可用环境变量覆盖（逗号分隔）
    import os as _os

    _env_origins = [
        o.strip() for o in _os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_env_origins or settings.server.cors_origins,
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

    # 管理端（控制面）：词库管理。仅本机可用，与客户端完全解耦。
    from app.admin import page_router as admin_page_router  # noqa: PLC0415
    from app.admin import router as admin_router  # noqa: PLC0415

    from app.app_release import router as app_release_router  # noqa: PLC0415

    app.include_router(app_release_router)
    app.include_router(admin_router)
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
    uploads_dir = os.path.join(str(settings.data_path), "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

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
