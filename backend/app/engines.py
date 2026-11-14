"""引擎装配层：把 config → 各引擎实例，供 FastAPI app.state 使用。

设计要点
--------
- **构造不加载权重**：STT/TTS 在此只创建对象，真正的模型加载在 lifespan 的
  ``prewarm()``（后台任务）里做，避免冷启动阻塞健康检查。
- **优雅降级**：任何引擎在缺失依赖/模型时都能退到 mock/tone 后端，
  使得整条链路在没有 GPU、没有模型、没有 LLM key 的机器上依然可跑通。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.llm.client import LLMClient
from app.llm.suggestions import SuggestionService
from app.logging_utils import get_logger
from app.personas import PersonaLibrary, get_personas
from app.voice.ref_audio import ReferenceLibrary
from app.voice.stt import STTEngine
from app.voice.tts import TTSEngine

log = get_logger(__name__)


@dataclass
class Engines:
    stt: STTEngine
    tts: TTSEngine
    llm: LLMClient
    personas: PersonaLibrary
    suggestions: SuggestionService
    ref_audio: ReferenceLibrary
    settings: Settings
    llm_config: Any = None
    #: 运行时 STT 配置（本地 ↔ 硅基流动 SenseVoiceSmall 热切换）
    stt_config: Any = None
    #: 声音克隆引擎（XTTS-v2）。惰性创建，只在用户上传音色时才加载模型。
    xtts: Any = None
    #: 声音转换引擎（OpenVoice V2）。与 xtts 配合实现「保留口音」的克隆：
    #: edge 母语声线（真口音）→ OpenVoice（换成用户音色）。
    openvoice: Any = None
    #: 角色 → 上传音色 的绑定（persona_id → voice_id）
    voice_assignments: dict[str, str] = field(default_factory=dict)
    _warm: dict[str, str] = field(default_factory=dict)

    # ---- 运行时重配 LLM ----
    async def apply_llm_config(self, **changes: Any) -> dict[str, Any]:
        """更新 LLM 配置（可选校验），热重建 LLMClient。

        返回 ``{"config": public_view, "verified": {...}|None}``。
        重建时保持同一个 ``LLMClient`` 类的行为契约，因此 orchestrator 无需感知。
        """
        from app.llm import runtime_config as rc

        if self.llm_config is None:
            self.llm_config = rc.LLMConfigStore(
                self.settings.data_path,
                env_defaults={
                    "base_url": self.settings.llm.base_url,
                    "api_key": self.settings.llm.api_key,
                    "model": self.settings.llm.model,
                    "suggestion_model": self.settings.llm.suggestion_model,
                    "temperature": self.settings.llm.temperature,
                    "max_tokens": self.settings.llm.max_tokens,
                },
            )

        cfg = self.llm_config.save(**changes)

        verified: dict[str, Any] | None = None
        if changes.get("verify"):
            verified = await rc.verify_config(cfg)

        # 把配置写进 settings，然后重建客户端（旧客户端异步关闭）
        rc.apply_to_settings(self.settings.llm, cfg)
        old = self.llm
        self.llm = LLMClient(self.settings.llm)
        self.llm.usage_budget = getattr(old, 'usage_budget', None)
        self.llm.provider_pool = getattr(old, 'provider_pool', None)
        self.suggestions = SuggestionService(
            self.llm, self.personas, self.settings.llm, self.settings.pipeline
        )
        try:
            await old.aclose()
        except Exception as exc:  # pragma: no cover
            log.debug("close old llm client failed: %s", exc)

        log.info(
            "LLM 已重配: base_url=%s model=%s key=%s",
            self.settings.llm.base_url,
            self.settings.llm.model,
            "set" if self.settings.llm.api_key else "empty",
        )
        return {"config": cfg.public(), "verified": verified}

    # ---- 状态 ----
    def snapshot(self) -> dict[str, Any]:
        """给 /api/health 与 WS ready 用的引擎状态快照。"""
        health = self._safe(lambda: self.llm.health(), {})
        cfg_public = None
        if self.llm_config is not None:
            try:
                cfg_public = self.llm_config.public()
            except Exception:  # pragma: no cover
                cfg_public = None
        return {
            "stt": self._safe(lambda: self.stt.backend_name, "unknown"),
            "tts": self._safe(lambda: self.tts.backend_name, "unknown"),
            "llm": health,
            "llm_config": cfg_public,
            "vad": self.settings.vad.backend,
            "warm": dict(self._warm),
            # 克隆能力：cascade 能在换音色的同时保留真实口音
            "clone": self._safe(
                lambda: {
                    **self.tts.clonable_backends(),
                    "assignments": dict(self.voice_assignments),
                },
                {},
            ),
        }

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - 防御性
            log.debug("engine snapshot failed: %s", exc)
            return default

    # ---- 预热 ----
    async def prewarm(self, what: str = "all") -> dict[str, str]:
        """后台预热。每项独立 try/except，一个失败不影响其它。"""
        import asyncio

        async def _run(name: str, coro_factory) -> None:
            try:
                await coro_factory()
                self._warm[name] = "ready"
                log.info("prewarm[%s] ready", name)
            except Exception as exc:
                self._warm[name] = f"failed: {type(exc).__name__}"
                log.warning("prewarm[%s] failed: %s", name, exc)

        tasks = []
        if what in ("all", "stt"):
            tasks.append(_run("stt", self.stt.prewarm))
        if what in ("all", "tts"):
            persona_ids = [p.id for p in self.personas.all()]
            tasks.append(_run("tts", lambda: self.tts.prewarm(persona_ids)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return dict(self._warm)

    async def aclose(self) -> None:
        for name, closer in (("stt", self.stt), ("tts", self.tts), ("llm", self.llm)):
            try:
                await closer.aclose()
            except Exception as exc:  # pragma: no cover
                log.debug("close %s failed: %s", name, exc)


def build_engines(settings: Settings | None = None) -> Engines:
    s = settings or get_settings()

    personas = get_personas()

    # 运行时 LLM 配置（文件 > .env），在构造客户端前应用，保证首启就用对端点
    from app.llm import runtime_config as rc

    llm_store = rc.LLMConfigStore(
        s.data_path,
        env_defaults={
            "base_url": s.llm.base_url,
            "api_key": s.llm.api_key,
            "model": s.llm.model,
            "suggestion_model": s.llm.suggestion_model,
            "temperature": s.llm.temperature,
            "max_tokens": s.llm.max_tokens,
        },
    )
    rc.apply_to_settings(s.llm, llm_store.config)

    stt = STTEngine(s.stt)

    # 运行时 STT 配置（文件 > .env > 默认），构造后立即应用，保证首启就用对后端
    from app.voice.stt_config import STTConfigStore  # noqa: PLC0415

    stt_store = STTConfigStore(
        s.data_path,
        env_defaults={
            "provider": s.stt.provider,
            "base_url": s.stt.siliconflow_base_url,
            "model": s.stt.siliconflow_model,
            "api_key": s.stt.siliconflow_api_key,
            "language": s.stt.language,
            "timeout_s": s.stt.siliconflow_timeout_s,
        },
    )
    apply_cloud = getattr(stt, "apply_cloud_config", None)
    if callable(apply_cloud):
        apply_cloud(stt_store.config.to_engine_dict())
    else:  # pragma: no cover - 兼容尚未接入云端 STT 的旧代码
        log.warning("STTEngine 尚未实现 apply_cloud_config，云端 STT 暂不可用")

    tts = TTSEngine(s.tts, personas=personas)
    llm = LLMClient(s.llm)
    ref_audio = ReferenceLibrary(s.ref_audio_path)
    suggestions = SuggestionService(llm, personas, s.llm, s.pipeline)

    # ---- OpenVoice（声音转换）：让克隆保留真实口音 ----
    # 关键架构：XTTS 直接克隆会丢掉口音（只保留音色），
    # 所以「上传音色」场景走级联：edge 母语声线（真口音）→ OpenVoice（换音色）。
    # 见 docs/VOICE_CLONE.md「为什么口音能保留」。
    openvoice = None
    try:
        from app.voice.openvoice_engine import OpenVoiceEngine  # noqa: PLC0415

        if OpenVoiceEngine.looks_available():
            openvoice = OpenVoiceEngine(device=s.tts.device)
            tts.bind_openvoice(openvoice)
            log.info("OpenVoice V2 已接入：克隆将保留真实口音（级联架构）")
        else:
            log.info("OpenVoice V2 不可用：克隆回退到 XTTS（口音会丢）")
    except Exception as exc:  # noqa: BLE001
        log.warning("OpenVoice 初始化失败（忽略）：%s", exc)

    stt_cfg = stt_store.config
    log.info(
        "engines built: stt=%s (cloud_provider=%s enabled=%s configured=%s) "
        "tts=%s llm=%s (configured=%s, source=%s)",
        stt.backend_name,
        stt_cfg.provider,
        stt_cfg.enabled,
        stt_cfg.configured,
        tts.backend_name,
        s.llm.model,
        bool(s.llm.api_key),
        llm_store.config.source,
    )
    return Engines(
        stt=stt,
        tts=tts,
        llm=llm,
        openvoice=openvoice,
        personas=personas,
        suggestions=suggestions,
        ref_audio=ref_audio,
        settings=s,
        llm_config=llm_store,
        stt_config=stt_store,
    )


def gpu_info() -> dict[str, Any]:
    """尽力而为地报告 GPU 情况，供 /api/health 使用。"""
    info: dict[str, Any] = {"available": False}
    try:
        import torch  # noqa: PLC0415

        info["torch"] = torch.__version__
        info["available"] = bool(torch.cuda.is_available())
        if info["available"]:
            idx = torch.cuda.current_device()
            props = torch.cuda.get_device_properties(idx)
            info.update(
                {
                    "name": props.name,
                    "total_vram_gb": round(props.total_memory / 1024**3, 1),
                    "cuda": torch.version.cuda,
                    "capability": f"{props.major}.{props.minor}",
                }
            )
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info
