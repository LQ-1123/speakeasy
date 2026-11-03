"""运行时 STT 配置（本地 Whisper ↔ 硅基流动 SenseVoiceSmall）。

问题
----
``STT_BACKEND`` 等嵌套配置项只能靠改 ``.env`` 再重启；而用户希望在「个人设置」里
填一个硅基流动 API Key 就立刻切到云端 SenseVoiceSmall。

方案
----
- 存储：``data/stt_config.json``（0600），优先级 文件 > 环境变量 > 默认。
- 热生效：保存后调用 ``STTEngine.apply_cloud_config`` 重建云端客户端，无需重启。
- 安全：api_key 只落盘，接口只回传 ``sk-a…2345`` 指纹；留空=不改，``__CLEAR__``=清空。
- 降级：云端失败由 ``STTEngine`` 自动回退本地 faster-whisper → mock。
"""

from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

CONFIG_FILENAME = "stt_config.json"

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"

#: 前端选择器用的预设
STT_PRESETS: list[dict[str, Any]] = [
    {
        "id": "local",
        "name": "本地 Whisper（离线）",
        "base_url": "",
        "model": "distil-large-v3",
        "note": "本机 CPU/GPU 推理，无需网络与 Key；准确率高但较慢",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 SenseVoiceSmall",
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "key_hint": "sk-...",
        "note": "云端转写，中文/英文/多语种；需 SiliconFlow API Key",
    },
]

_VALID_PROVIDERS = ("auto", "local", "siliconflow")


@dataclass
class STTRuntimeConfig:
    """可热更新的 STT 配置。"""

    provider: str = "auto"          # auto | local | siliconflow
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = ""
    language: str = "en"
    timeout_s: float = 30.0
    source: str = field(default="env", compare=False)

    # ---------------- 语义 ----------------
    @property
    def enabled(self) -> bool:
        """是否启用云端 STT（local 永远关闭；没有 key 时即使 provider=siliconflow 也不启用）。"""
        return self.provider != "local" and bool(self.api_key.strip())

    @property
    def configured(self) -> bool:
        """云端是否具备可用配置（enabled + base_url + model + key）。"""
        return bool(
            self.enabled
            and self.base_url.strip()
            and self.model.strip()
            and self.api_key.strip()
        )

    def masked_key(self) -> str:
        k = self.api_key.strip()
        if not k:
            return ""
        if len(k) <= 8:
            return "*" * len(k)
        return f"{k[:4]}…{k[-4:]}"

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "language": self.language,
            "timeout_s": self.timeout_s,
            "api_key_set": bool(self.api_key.strip()),
            "api_key_masked": self.masked_key(),
            "configured": self.configured,
            "source": self.source,
        }

    def to_engine_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "api_key": self.api_key,
            "language": self.language,
            "timeout_s": self.timeout_s,
        }

    def to_file_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("source", None)
        return d


def _norm_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _clamp_timeout(value: Any, default: float = 30.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(5.0, min(120.0, v))


class STTConfigStore:
    """配置加载 / 保存 / 脱敏（线程安全）。"""

    def __init__(self, data_dir: str, *, env_defaults: dict[str, Any] | None = None) -> None:
        self.path = Path(data_dir) / CONFIG_FILENAME
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._env = env_defaults or {}
        self._cfg = self._load()

    # ---------------- 读 ----------------
    def _load(self) -> STTRuntimeConfig:
        cfg = STTRuntimeConfig(source="env")
        env = self._env
        provider = str(env.get("provider") or "auto").strip().lower()
        cfg.provider = provider if provider in _VALID_PROVIDERS else "auto"
        if env.get("base_url"):
            cfg.base_url = _norm_base_url(str(env["base_url"]))
        if env.get("model"):
            cfg.model = str(env["model"]).strip()
        if env.get("api_key"):
            cfg.api_key = str(env["api_key"]).strip()
        if env.get("language"):
            cfg.language = str(env["language"]).strip() or "en"
        cfg.timeout_s = _clamp_timeout(env.get("timeout_s"), cfg.timeout_s)

        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                provider = str(raw.get("provider") or cfg.provider).strip().lower()
                cfg.provider = provider if provider in _VALID_PROVIDERS else "auto"
                if raw.get("base_url") is not None:
                    cfg.base_url = _norm_base_url(str(raw["base_url"]))
                if raw.get("model") is not None:
                    cfg.model = str(raw["model"]).strip()
                if raw.get("api_key") is not None:
                    cfg.api_key = str(raw["api_key"]).strip()
                if raw.get("language") is not None:
                    cfg.language = str(raw["language"]).strip() or "en"
                if raw.get("timeout_s") is not None:
                    cfg.timeout_s = _clamp_timeout(raw["timeout_s"], cfg.timeout_s)
                cfg.source = "file"
                log.info(
                    "已加载运行时 STT 配置: provider=%s model=%s key=%s",
                    cfg.provider, cfg.model, "set" if cfg.api_key else "empty",
                )
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                log.warning("运行时 STT 配置读取失败（忽略，用环境变量）: %s", exc)
        else:
            log.info("无运行时 STT 配置，使用 .env/默认（provider=%s）", cfg.provider)
        return cfg

    @property
    def config(self) -> STTRuntimeConfig:
        with self._lock:
            return self._cfg

    def public(self) -> dict[str, Any]:
        return self.config.public()

    # ---------------- 写 ----------------
    def save(self, **changes: Any) -> STTRuntimeConfig:
        with self._lock:
            cfg = self._cfg
            if changes.get("provider") is not None:
                provider = str(changes["provider"]).strip().lower()
                if provider not in _VALID_PROVIDERS:
                    raise ValueError(f"provider 必须是 {', '.join(_VALID_PROVIDERS)}")
                cfg.provider = provider
            if changes.get("base_url") is not None:
                cfg.base_url = _norm_base_url(str(changes["base_url"])) or DEFAULT_BASE_URL
            if changes.get("model") is not None:
                cfg.model = str(changes["model"]).strip() or DEFAULT_MODEL
            if changes.get("language") is not None:
                cfg.language = str(changes["language"]).strip() or "en"
            if changes.get("timeout_s") is not None:
                cfg.timeout_s = _clamp_timeout(changes["timeout_s"], cfg.timeout_s)
            if changes.get("api_key") is not None:
                key = str(changes["api_key"]).strip()
                if key == "__CLEAR__":
                    cfg.api_key = ""
                elif key:
                    cfg.api_key = key
                # 空字符串 = 保持原值（前端不回传明文）
            cfg.source = "file"
            self._write_locked(cfg)
            return cfg

    def _write_locked(self, cfg: STTRuntimeConfig) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(cfg.to_file_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            log.info("已保存运行时 STT 配置: provider=%s model=%s", cfg.provider, cfg.model)
        except OSError as exc:
            log.error("保存运行时 STT 配置失败: %s", exc)
            raise

    def reset(self) -> STTRuntimeConfig:
        with self._lock:
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError as exc:
                log.warning("删除 STT 配置文件失败: %s", exc)
            self._cfg = self._load()
            return self._cfg


async def verify_config(cfg: STTRuntimeConfig, *, transport: Any = None) -> dict[str, Any]:
    """真发一次极小的转写请求验证 key/base_url/model。永远返回 dict，不抛。"""
    if not cfg.configured:
        return {
            "ok": False,
            "message": "请先选择「硅基流动 SenseVoiceSmall」并填写 API Key",
            "latency_ms": 0.0,
            "text": "",
        }
    try:
        from app.voice.sensevoice import SiliconFlowSTT  # noqa: PLC0415 - 延迟 import
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"SenseVoice 模块不可用: {exc}",
            "latency_ms": 0.0,
            "text": "",
        }
    client = SiliconFlowSTT(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        timeout_s=cfg.timeout_s,
        language=cfg.language,
        transport=transport,
    )
    try:
        return await client.verify()
    except Exception as exc:  # noqa: BLE001 - 校验端点不能炸
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "latency_ms": 0.0,
            "text": "",
        }
