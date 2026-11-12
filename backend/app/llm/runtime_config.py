"""运行时 LLM 配置：可在界面里填写 base_url / api_key / model，即时生效并持久化。

为什么需要它
------------
传统做法是改 ``.env`` 再重启进程——对"想马上试试另一个 API 供应商"的用户太笨重。
这里提供一个**可热更新**的配置层：

1. 启动时以 ``.env`` 为默认值。
2. 界面通过 ``POST /api/llm/config`` 提交新配置 → 校验 → 写盘 → 重建 LLM 客户端。
3. 配置存在 ``data/llm_config.json``（git 忽略），优先级高于环境变量。
4. API key 在响应里**永远脱敏**，只返回 ``sk-...abcd`` 这样的指纹与是否已设置。

安全边界
--------
- key 只写在本机文件，不回传前端明文（前端输入框是只写不读）。
- 文件权限尽力收紧（POSIX 下 0600）。
- 校验会**实际发一次极小的请求**确认可用，避免"存了个错的配置还以为好了"。
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

CONFIG_FILENAME = "llm_config.json"

#: 常见供应商预设，前端可直接展示成下拉
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_hint": "sk-...",
        "note": "国内直连，英文表达自然，价格低",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_hint": "sk-proj-...",
        "note": "口语化程度高，需科学上网",
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow 硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "key_hint": "sk-...",
        "note": "国内聚合，模型选择多",
    },
    {
        "id": "moonshot",
        "name": "Moonshot Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "key_hint": "sk-...",
        "note": "国内直连",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_hint": "...",
        "note": "国内直连，glm-4-flash 有免费额度",
    },
    {
        "id": "ollama",
        "name": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "key_hint": "留空或任意值",
        "note": "完全本地，无需 key",
    },
    {
        "id": "custom",
        "name": "自定义（任意 OpenAI 兼容端点）",
        "base_url": "",
        "model": "",
        "key_hint": "按供应商要求",
        "note": "one-api / new-api / vLLM / LM Studio 等",
    },
]


@dataclass
class LLMRuntimeConfig:
    """可热更新的 LLM 配置（与 ``LLMSettings`` 同构的子集）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    suggestion_model: str = ""
    #: 直接透传给 OpenAI 兼容接口的额外字段（例如 Qwen3 关闭 thinking）。
    extra_body: dict[str, Any] = field(default_factory=dict)

    # 来源标记（不持久化，仅供排查）
    source: str = field(default="env", compare=False)

    def __post_init__(self) -> None:
        """所有构造路径统一规范化：只填域名时补 /v1，去掉粘贴带进来的空白。"""
        self.base_url = _norm_base_url(str(self.base_url or ""))
        self.api_key = str(self.api_key or "").strip()
        self.model = str(self.model or "").strip()
        self.suggestion_model = str(self.suggestion_model or "").strip()
        if not isinstance(self.extra_body, dict):
            self.extra_body = {}

    def is_configured(self) -> bool:
        """有 base_url + model 即视为可尝试；key 是否必需取决于供应商（本地模型可空）。"""
        return bool(self.base_url.strip() and self.model.strip())

    def masked_key(self) -> str:
        """脱敏后的 key 指纹；未设置返回空串。"""
        k = self.api_key.strip()
        if not k:
            return ""
        if len(k) <= 8:
            return "*" * len(k)
        return f"{k[:4]}…{k[-4:]}"

    def public(self) -> dict[str, Any]:
        """给前端的安全视图（**绝不包含 key 明文**）。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "suggestion_model": self.suggestion_model,
            "extra_body": dict(self.extra_body),
            "api_key_set": bool(self.api_key.strip()),
            "api_key_masked": self.masked_key(),
            "configured": self.is_configured(),
            "source": self.source,
        }

    def to_file_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("source", None)
        return d


def _norm_base_url(url: str) -> str:
    """补全并规范化 base_url（OpenAI 兼容端点通常需要 ``/v1``）。"""
    from urllib.parse import urlsplit  # noqa: PLC0415

    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    parts = urlsplit(u)
    path = (parts.path or "").rstrip("/")
    # 用户把完整端点粘进来时削回 base：…/v1/chat/completions → …/v1
    for suffix in ("/chat/completions", "/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    # 只填域名（或 http://host/）时补 /v1：OpenAI SDK 会拼 /chat/completions
    if path in ("", "/"):
        path = "/v1"
    return f"{parts.scheme}://{parts.netloc}{path}" + (f"?{parts.query}" if parts.query else "")


class LLMConfigStore:
    """配置的加载 / 保存 / 校验 / 脱敏。线程安全（Web 多连接并发提交）。"""

    def __init__(self, data_dir: str, *, env_defaults: dict[str, Any] | None = None) -> None:
        self.path = Path(data_dir) / CONFIG_FILENAME
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._env = env_defaults or {}
        self._cfg: LLMRuntimeConfig = self._load()

    # ---------------- 读 ----------------
    def _load(self) -> LLMRuntimeConfig:
        """文件 > 环境变量 > 内置默认。"""
        cfg = LLMRuntimeConfig(source="env")
        for k in ("base_url", "api_key", "model", "suggestion_model"):
            v = self._env.get(k)
            if v:
                setattr(cfg, k, str(v))
        if self._env.get("temperature") is not None:
            cfg.temperature = float(self._env["temperature"])
        if self._env.get("max_tokens") is not None:
            cfg.max_tokens = int(self._env["max_tokens"])

        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for k in ("base_url", "api_key", "model", "suggestion_model"):
                    if raw.get(k):
                        setattr(cfg, k, str(raw[k]))
                if isinstance(raw.get("extra_body"), dict):
                    cfg.extra_body = dict(raw["extra_body"])
                if raw.get("temperature") is not None:
                    cfg.temperature = float(raw["temperature"])
                if raw.get("max_tokens") is not None:
                    cfg.max_tokens = int(raw["max_tokens"])
                cfg.source = "file"
                log.info("已加载运行时 LLM 配置: %s / %s", cfg.base_url, cfg.model)
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                log.warning("运行时 LLM 配置读取失败（忽略，用环境变量）: %s", exc)
        else:
            log.info("无运行时 LLM 配置，使用 .env: %s / %s", cfg.base_url, cfg.model)
        return cfg

    @property
    def config(self) -> LLMRuntimeConfig:
        with self._lock:
            return self._cfg

    def public(self) -> dict[str, Any]:
        return self.config.public()

    # ---------------- 写 ----------------
    def save(self, **changes: Any) -> LLMRuntimeConfig:
        """更新并持久化；只接受显式传入的字段。"""
        with self._lock:
            cfg = self._cfg
            if "base_url" in changes and changes["base_url"] is not None:
                cfg.base_url = _norm_base_url(str(changes["base_url"]))
            if "model" in changes and changes["model"] is not None:
                cfg.model = str(changes["model"]).strip()
            if "suggestion_model" in changes and changes["suggestion_model"] is not None:
                cfg.suggestion_model = str(changes["suggestion_model"]).strip()
            if "temperature" in changes and changes["temperature"] is not None:
                cfg.temperature = float(changes["temperature"])
            if "max_tokens" in changes and changes["max_tokens"] is not None:
                cfg.max_tokens = int(changes["max_tokens"])
            if "extra_body" in changes and isinstance(changes["extra_body"], dict):
                cfg.extra_body = dict(changes["extra_body"])
            # key 单独处理：空字符串 = 不改；"_CLEAR_" = 清空
            if "api_key" in changes and changes["api_key"] is not None:
                k = str(changes["api_key"]).strip()
                if k == "__CLEAR__":
                    cfg.api_key = ""
                elif k:
                    cfg.api_key = k
                # 空字符串：保持原值（前端不回传明文，避免误清空）

            cfg.source = "file"
            self._write_locked(cfg)
            return cfg

    def _write_locked(self, cfg: LLMRuntimeConfig) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = cfg.to_file_dict()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            # 尽力收紧权限（含 key，别让同机其他用户读到）
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Windows 上通常不支持，忽略
            log.info("已保存运行时 LLM 配置: %s / %s", cfg.base_url, cfg.model)
        except OSError as exc:
            log.error("保存运行时 LLM 配置失败: %s", exc)
            raise

    def reset(self) -> LLMRuntimeConfig:
        """删除文件，回到环境变量默认。"""
        with self._lock:
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError as exc:
                log.warning("删除配置文件失败: %s", exc)
            self._cfg = self._load()
            return self._cfg


# --------------------------------------------------------------------------- #
# 应用到一个 LLMSettings
# --------------------------------------------------------------------------- #
def apply_to_settings(settings: Any, cfg: LLMRuntimeConfig) -> Any:
    """把运行时配置写进 ``LLMSettings`` 实例（原地修改，返回同一对象）。"""
    if cfg.base_url:
        settings.base_url = cfg.base_url
    if cfg.model:
        settings.model = cfg.model
    if cfg.api_key:
        settings.api_key = cfg.api_key
    elif cfg.source == "file" and not cfg.api_key:
        # 文件里显式清空过 key
        settings.api_key = ""
    if cfg.temperature is not None:
        settings.temperature = float(cfg.temperature)
    if cfg.max_tokens is not None:
        settings.max_tokens = int(cfg.max_tokens)
    if cfg.suggestion_model:
        settings.suggestion_model = cfg.suggestion_model
    if cfg.extra_body:
        settings.extra_body = dict(cfg.extra_body)
    return settings


async def verify_config(cfg: LLMRuntimeConfig, *, timeout_s: float = 20.0) -> dict[str, Any]:
    """真的发一次极小请求，确认 base_url / key / model 可用。

    返回 ``{"ok": bool, "message": str, "latency_ms": float, "model_reply": str}``。
    不抛异常——所有失败都折叠成 ok=False + 可读 message。
    """
    import time as _t

    from app.llm.client import LLMConfigError, LLMError  # 延迟 import 避免循环

    if not cfg.is_configured():
        return {"ok": False, "message": "base_url 与 model 都必须填写", "latency_ms": 0.0, "model_reply": ""}

    t0 = _t.perf_counter()
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "not-needed",
            timeout=timeout_s,
            max_retries=0,
        )
        try:
            request_kwargs: dict[str, Any] = {
                "model": cfg.model,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "max_tokens": 64,
                "temperature": 0,
            }
            if cfg.extra_body:
                request_kwargs["extra_body"] = dict(cfg.extra_body)
            resp = await client.chat.completions.create(**request_kwargs)
        finally:
            await client.close()

        txt = ""
        try:
            txt = (resp.choices[0].message.content or "").strip()
        except (AttributeError, IndexError):
            txt = ""
        return {
            "ok": True,
            "message": f"连接成功，模型 {cfg.model} 正常响应",
            "latency_ms": round((_t.perf_counter() - t0) * 1000, 1),
            "model_reply": txt[:80],
        }
    except Exception as exc:  # noqa: BLE001 - 校验端点必须给出可读原因
        name = type(exc).__name__
        raw = str(exc)
        low = raw.lower()

        if "401" in raw or "invalid_api_key" in low or "unauthorized" in low or isinstance(exc, LLMConfigError):
            msg = (
                f"API Key 无效、过期或复制不完整（{name}）：{raw[:160]}"
                f"（当前 base_url={cfg.base_url}）"
            )
        elif "404" in raw or "model_not_found" in low or "does not exist" in low:
            msg = (
                f"端点或模型不存在（{name}）：{raw[:160]}"
                f"（base_url 通常需要以 /v1 结尾，当前 {cfg.base_url}）"
            )
        elif "connect" in low or "timeout" in low or "timed out" in low or "getaddrinfo" in low:
            msg = f"连不上 base_url（{name}）：{raw[:160]}"
        elif "429" in raw or "rate" in low or "quota" in low:
            msg = f"限流或额度不足（{name}）：{raw[:160]}"
        elif isinstance(exc, LLMError):
            msg = f"LLM 调用失败（{name}）：{raw[:160]}"
        else:
            msg = f"{name}：{raw[:200]}"

        return {
            "ok": False,
            "message": msg,
            "latency_ms": round((_t.perf_counter() - t0) * 1000, 1),
            "model_reply": "",
        }


__all__ = [
    "LLMConfigStore",
    "LLMRuntimeConfig",
    "PROVIDER_PRESETS",
    "apply_to_settings",
    "verify_config",
]
