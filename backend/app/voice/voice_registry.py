"""Persona 音色的**唯一来源**：一个角色 = 一个声音。

为什么需要这个模块
------------------
修复前，同一个人物在不同链路会解析出不同声线：

* 聊天：``friend.fish_voice_id or 全局参考音色`` → 失败回落 ``friend.voice_id``（Edge）
* 动态朗读：``persona.fish_voice_id or 全局参考音色`` → 失败把 **Fish 音色 id 当成 Edge 声线**（必失败→tone）
* 试听：又一个独立实现

Fish 一不可用，这三条链路就各自回落到不同声音，用户听到「同一个人两个嗓音」。

本模块规则
----------
1. 角色有 ``fish_voice_id`` 且 Fish 可用 → **Fish**（来自 Fish 音色库，一人一色）。
2. 否则 / Fish 失败 → **该角色自己的 Edge 声线**（``personas.voice_id``，合法 Edge 名；不合法则按语言+性别确定性挑一个）。
3. 再失败 → 交给调用方的 tone 兜底。

**禁止**任何链路再自己拼 ``reference_id``；统一走 :func:`synthesize`。

另外内置一个短的 Fish「熔断」窗口：连接失败后 45 秒内直接走 Edge，
避免每次请求都先等十几秒超时（这是聊天卡顿的主因之一）。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from app.logging_utils import get_logger
from app.v3.seed_data import voice_for_language

log = get_logger(__name__)


def get_pool():
    from app.providers.runtime import get_pool as current_pool

    return current_pool()


def _fish_pool(cfg: Any = None):
    pool = get_pool()
    model = str(getattr(cfg, "fish_model", "") or "s2.1-pro")
    return pool if pool is not None and pool.applies("fish", model) else None

#: 合法 Edge 声线名（例：en-GB-RyanNeural / de-DE-FlorianMultilingualNeural）
_EDGE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+(?:Multilingual)?Neural$")

#: Fish 连接失败后的冷却窗口（秒）：窗口内直接走 Edge，不再等超时
FISH_DOWN_SECONDS = 45.0

_FISH_STATE: dict[str, Any] = {
    "down_until": 0.0,
    "last_error": "",
    "last_ok": 0.0,
    # 最近一次真实尝试是否成功：None=还没试过（乐观），False=最近失败（统一降级）
    "attempt_ok": None,
}


# --------------------------------------------------------------------------- #
# 配置 / 状态
# --------------------------------------------------------------------------- #
def load_cfg(store: Any = None) -> Any:
    """读取 TTS 配置；传入 store 则复用（chat_service 自带 store）。

    ``SE_TTS_FISH=0`` 可整体关闭 Fish（测试/离线环境），此时所有链路统一走 Edge。
    """
    if str(os.getenv("SE_TTS_FISH", "1")).strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        if store is not None:
            return store.load()
        from app.config import get_settings  # noqa: PLC0415
        from app.voice.tts_config import TTSConfigStore  # noqa: PLC0415

        return TTSConfigStore(str(get_settings().data_path)).load()
    except Exception as exc:  # noqa: BLE001 - 配置读不到就按未启用处理
        log.debug("读取 TTS 配置失败: %s", exc)
        return None


def fish_configured(cfg: Any) -> bool:
    # SE_TTS_FISH=0（测试/离线）时视为未配置：所有调用点都不再联网
    if str(os.getenv("SE_TTS_FISH", "1")).strip().lower() in ("0", "false", "no", "off"):
        return False
    if _fish_pool(cfg) is not None:
        return True
    return bool(
        cfg is not None
        and str(getattr(cfg, "provider", "") or "") == "fish"
        and str(getattr(cfg, "fish_api_key", "") or "").strip()
    )


def fish_usable(cfg: Any, *, now: float | None = None) -> bool:
    """配置可用且不在熔断窗口内。

    刚启动（未知）或冷却结束 → 允许试一次 Fish；失败会重新进入熔断窗口，
    成功由 ``mark_fish_success`` 清零。这样冷启动不会永远只用 Edge，
    失败后也能自动恢复（否则重启后 ``attempt_ok=None`` 就再也轮不到 Fish）。
    """
    if not fish_configured(cfg):
        return False
    if _fish_pool(cfg) is not None:
        # A route retains ownership while accounts cool or drain. Acquisition
        # waits/rejects inside the pool instead of selecting a legacy account.
        return True
    return (now or time.time()) >= float(_FISH_STATE.get("down_until") or 0.0)


def mark_fish_failure(exc: Any) -> None:
    if _fish_pool(load_cfg()) is not None:
        return
    _FISH_STATE["down_until"] = time.time() + FISH_DOWN_SECONDS
    _FISH_STATE["last_error"] = f"{type(exc).__name__}: {exc}"[:240]
    _FISH_STATE["attempt_ok"] = False


def mark_fish_success() -> None:
    if _fish_pool(load_cfg()) is not None:
        return
    _FISH_STATE["down_until"] = 0.0
    _FISH_STATE["last_ok"] = time.time()
    _FISH_STATE["attempt_ok"] = True


async def probe(cfg: Any = None) -> bool:
    """轻量探测：只有真连通才把 Fish 切回来（不合成音频）。"""
    cfg = cfg if cfg is not None else load_cfg()
    if not fish_configured(cfg):
        return False
    pool = _fish_pool(cfg)
    if pool is not None:
        # Resource verification is an explicit business probe in providers;
        # do not let a catalog request rewrite per-account health.
        available = getattr(pool, "available", None)
        return bool(available("fish", str(getattr(cfg, "fish_model", "") or "s2.1-pro"))) if available else True
    if (time.time()) < float(_FISH_STATE.get("down_until") or 0.0):
        return False
    try:
        from app.voice import fish_tts  # noqa: PLC0415

        await fish_tts.list_models(
            api_key=str(getattr(cfg, "fish_api_key", "") or ""),
            base_url=str(getattr(cfg, "fish_base_url", "") or ""),
            page_size=1,
            timeout=4.0,
            use_cache=False,
        )
        mark_fish_success()
        return True
    except Exception as exc:  # noqa: BLE001
        mark_fish_failure(exc)
        return False


def fish_status() -> dict:
    """给 /health、/stats 用的诊断快照（不含任何密钥）。"""
    cfg = load_cfg()
    pool = _fish_pool(cfg)
    if pool is not None:
        model = str(getattr(cfg, "fish_model", "") or "s2.1-pro")
        available = getattr(pool, "available", None)
        reachable = bool(available("fish", model)) if available is not None else True
        return {"reachable": reachable, "retry_in_s": 0.0, "last_ok_at": 0.0,
                "last_error": "", "attempt_ok": None, "source": "pool"}
    now = time.time()
    down_until = float(_FISH_STATE.get("down_until") or 0.0)
    return {
        "reachable": down_until <= now,
        "retry_in_s": round(max(0.0, down_until - now), 1),
        "last_ok_at": _FISH_STATE.get("last_ok") or 0.0,
        "last_error": _FISH_STATE.get("last_error") or "",
        "attempt_ok": _FISH_STATE.get("attempt_ok"),
    }


def reset_state() -> None:
    """测试用：清掉熔断状态。"""
    _FISH_STATE.update({"down_until": 0.0, "last_error": "", "last_ok": 0.0, "attempt_ok": None})


# --------------------------------------------------------------------------- #
# 绑定
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VoiceBinding:
    """一个角色的声音身份；四链路必须用同一个 binding。

    ``token`` 是**稳定身份**（有 Fish 音色就是 ``fish-<hash>``，否则 ``edge-<声线>``），
    不随网络可用性变化——聊天/朗读/通话/试听因此永远对比得到同一个 token。
    实际渲染用什么后端看 ``provider``，缓存文件名用 ``render_key``（含 provider），
    这样降级期间的 Edge 音频不会被 Fish 恢复后的请求误命中。
    """

    persona_id: str
    provider: str  # 本次实际尝试的后端："fish" | "edge"
    ref: str  # provider=fish 时的 Fish 音色 id
    edge_voice: str  # provider=edge 时（或 Fish 失败时）的 Edge 声线
    token: str  # 稳定身份：fish-<hash8> / edge-<voice>
    strict: bool = False

    @property
    def fish_enabled(self) -> bool:
        return self.provider == "fish" and bool(self.ref)

    @property
    def render_key(self) -> str:
        """缓存键：身份 + 实际后端（降级产物与正式产物分文件）。"""
        return f"{self.provider}-{self.token}"


def edge_voice_valid(name: str) -> bool:
    return bool(_EDGE_RE.match(str(name or "").strip()))


def render_revision(persona: dict, cfg: Any = None) -> str:
    """Version public URLs and on-disk artifacts with the same synthesis inputs."""
    cfg = cfg if cfg is not None else load_cfg()
    parameters = "|".join((
        "binding-v2", str(getattr(cfg, "fish_model", "") or ""),
        str(getattr(cfg, "fish_format", "") or ""),
        str(persona.get("voice_rate") or "+0%"), str(persona.get("voice_pitch") or "+0Hz"),
    ))
    return hashlib.sha1(parameters.encode("utf-8")).hexdigest()[:8]


def valid_edge_voice(persona: dict) -> str:
    """角色固定下来的 Edge 声线：优先 persona.voice_id，不合法就确定性挑一个。"""
    persona = persona if isinstance(persona, dict) else {}
    own = str(persona.get("voice_id") or "").strip()
    if edge_voice_valid(own):
        return own
    return voice_for_language(
        str(persona.get("language") or "en").strip().lower() or "en",
        seed=str(persona.get("id") or ""),
        gender=str(persona.get("gender") or ""),
    )


def binding_for_persona(persona: dict, cfg: Any = None, *, strict: bool | None = None) -> VoiceBinding:
    """persona 行 → 声音身份。

    ``token`` 是稳定身份（不随 Fish 是否可达变化）；``provider`` 才是本次实际会用的后端。
    """
    persona = persona if isinstance(persona, dict) else {}
    strict = strict if strict is not None else os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
    pid = str(persona.get("id") or "")
    if strict and (persona.get("status") == "waiting_voice" or (
        not str(persona.get("fish_voice_id") or "").strip() and not edge_voice_valid(str(persona.get("voice_id") or ""))
    )):
        raise ValueError("voice_not_ready")
    edge_voice = valid_edge_voice(persona)
    fish_id = str(persona.get("fish_voice_id") or "").strip()
    cfg = cfg if cfg is not None else load_cfg()
    if fish_id:
        digest = hashlib.sha1(fish_id.encode("utf-8")).hexdigest()[:8]
        token = f"fish-{digest}"
        provider = "fish" if strict or fish_usable(cfg) else "edge"
        return VoiceBinding(pid, provider, fish_id, edge_voice, token, strict)
    return VoiceBinding(pid, "edge", "", edge_voice, f"edge-{edge_voice}", strict)


def binding_for_friend(db: Any, friend: dict, cfg: Any = None, *, strict: bool | None = None) -> VoiceBinding:
    """好友行 → 声音身份。

    有 ``persona_id`` 时**实时读 personas 行**：好友行里的 fish_voice_id 是建好友时的快照，
    之后重新分配音色不会同步，聊天就会和动态朗读不一致。
    """
    friend = friend if isinstance(friend, dict) else {}
    pid = str(friend.get("persona_id") or "")
    if pid and db is not None:
        persona: dict | None = None
        try:
            # 只读查询，不取写锁：TTS 常在 to_thread 里跑，抢锁会拖慢甚至卡住回合
            row = db.conn.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone()
            persona = dict(row) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            log.debug("读取 persona 音色失败，回落好友行: %s", exc)
        if persona is not None:
            return binding_for_persona(persona, cfg, strict=strict)
    strict = strict if strict is not None else os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
    if strict:
        return binding_for_persona({**friend, "id": str(friend.get("id") or "")}, cfg, strict=True)
    # 非 Persona 好友（老数据 / 用户自建）：用好友行自己的字段
    edge_voice = str(friend.get("voice_id") or "").strip()
    if not edge_voice_valid(edge_voice):
        edge_voice = "en-GB-RyanNeural"
    fish_id = str(friend.get("fish_voice_id") or "").strip()
    cfg = cfg if cfg is not None else load_cfg()
    if fish_id:
        digest = hashlib.sha1(fish_id.encode("utf-8")).hexdigest()[:8]
        token = f"fish-{digest}"
        provider = "fish" if fish_usable(cfg) else "edge"
        return VoiceBinding(str(friend.get("id") or ""), provider, fish_id, edge_voice, token)
    return VoiceBinding(
        str(friend.get("id") or ""), "edge", "", edge_voice, f"edge-{edge_voice}"
    )


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
@dataclass
class SynthResult:
    data: bytes
    provider: str  # 实际使用的后端
    token: str
    ext: str  # "wav" | "mp3"
    duration_ms: int


def _estimate_duration_ms(data: bytes, ext: str) -> int:
    """粗估时长（只用于气泡长度）：mp3 按 128kbps，wav 按 24k/16bit。"""
    if not data:
        return 0
    if ext == "mp3":
        return max(500, int(len(data) * 8 / 128_000 * 1000))
    return max(300, int(len(data) / (24_000 * 2) * 1000))


# --------------------------------------------------------------------------- #
# Fish 合成结果磁盘缓存
# --------------------------------------------------------------------------- #
#: 同一角色 + 同一段文字 + 同一模型只合成一次。朗读按钮、信息流、聊天
#: 重复播放同一段时直接命中磁盘，避免每次都等 Fish 3–8 秒。
_TTS_CACHE_DIR = ".tts_cache"
_TTS_CACHE_MAX_FILES = 1200


def _tts_cache_enabled() -> bool:
    return str(os.getenv("SE_TTS_CACHE", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _tts_cache_key(
    binding: VoiceBinding,
    cfg: Any,
    text: str,
    ext: str,
    emotion_tag: str,
) -> str:
    payload = "|".join(
        (
            "tts-cache-v1",
            binding.token,
            binding.ref if binding.fish_enabled else "",
            str(getattr(cfg, "fish_model", "") or ""),
            str(getattr(cfg, "fish_base_url", "") or ""),
            ext,
            emotion_tag,
            text,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _tts_cache_path(out_dir: str, key: str, ext: str) -> str:
    return os.path.join(out_dir, _TTS_CACHE_DIR, f"{key}.{ext}")


def _tts_cache_read(out_dir: str, key: str, ext: str) -> bytes | None:
    if not out_dir or not _tts_cache_enabled():
        return None
    path = _tts_cache_path(out_dir, key, ext)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data if len(data) > 512 else None
    except OSError:
        return None


def _tts_cache_write(out_dir: str, key: str, ext: str, data: bytes) -> None:
    if not out_dir or not data or not _tts_cache_enabled():
        return
    cache_dir = os.path.join(out_dir, _TTS_CACHE_DIR)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        path = _tts_cache_path(out_dir, key, ext)
        tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        # 简单上限：超过上限时按 mtime 清掉最旧的 1/4，避免磁盘无限增长。
        try:
            names = [n for n in os.listdir(cache_dir) if not n.endswith(".tmp")]
            if len(names) > _TTS_CACHE_MAX_FILES:
                names.sort(key=lambda n: os.path.getmtime(os.path.join(cache_dir, n)))
                for name in names[: max(1, len(names) // 4)]:
                    os.remove(os.path.join(cache_dir, name))
        except OSError:
            pass
    except OSError as exc:  # noqa: BLE001 - 缓存失败绝不影响合成
        log.debug("TTS 缓存写入失败: %s", exc)


async def synthesize(
    persona_or_friend: dict,
    text: str,
    *,
    binding: VoiceBinding | None = None,
    cfg: Any = None,
    edge_engine: Any = None,
    emotion_tag: str = "",
    audio_format: str = "wav",
    out_dir: str = "",
) -> SynthResult | None:
    """按角色音色合成一段语音：Fish → 该角色的 Edge 声线 → None（调用方 tone 兜底）。

    ``binding`` 由调用方传入（聊天用 ``binding_for_friend``，朗读用 ``binding_for_persona``），
    保证同一角色四条链路拿到同一个声音。
    """
    text = str(text or "").strip()
    if not text:
        return None
    cfg = cfg if cfg is not None else load_cfg()
    if binding is None:
        binding = binding_for_persona(persona_or_friend, cfg)

    ext = (str(getattr(cfg, "fish_format", "") or audio_format) or "mp3").lower()
    cache_key = ""
    if binding.fish_enabled and out_dir:
        cache_key = _tts_cache_key(binding, cfg, _tagged(text, emotion_tag), ext, emotion_tag)
        cached = _tts_cache_read(out_dir, cache_key, ext)
        if cached is not None:
            return SynthResult(
                cached, "fish", binding.token, ext, _estimate_duration_ms(cached, ext)
            )

    # 1) Fish
    if binding.fish_enabled and fish_usable(cfg):
        try:
            from app.voice import fish_tts  # noqa: PLC0415

            data = await fish_tts.synthesize(
                _tagged(text, emotion_tag),
                api_key=str(getattr(cfg, "fish_api_key", "") or ""),
                base_url=str(getattr(cfg, "fish_base_url", "") or ""),
                model=str(getattr(cfg, "fish_model", "") or ""),
                reference_id=binding.ref,
                audio_format=ext,
                timeout=25.0,  # 单句上限：超时就降级，不拖住对话
            )
            if data:
                mark_fish_success()
                if cache_key:
                    _tts_cache_write(out_dir, cache_key, ext, data)
                return SynthResult(data, "fish", binding.token, ext, _estimate_duration_ms(data, ext))
        except Exception as exc:  # noqa: BLE001 - Fish 不可用是常态，统一降级
            mark_fish_failure(exc)
            log.warning("Fish 不可达（%s），统一降级到该角色的 Edge 声线 %s", exc, binding.edge_voice)

    if binding.strict and binding.fish_enabled:
        # A provider outage must remain explicit rather than changing the person's voice.
        return None

    # 2) Edge（角色自己的声线）
    if edge_engine is None:
        return None
    try:
        path, duration_ms = await edge_engine.synth_to_wav(
            _tagged(text, emotion_tag),
            binding.edge_voice,
            out_dir=out_dir or None,
        )
        with open(path, "rb") as fh:  # noqa: PTH123
            data = fh.read()
        try:
            os.remove(path)  # noqa: PTH107 - 调用方会自己落盘，临时文件删掉
        except OSError:
            pass
        if not data:
            return None
        return SynthResult(
            data, "edge", binding.token, "wav", int(duration_ms or _estimate_duration_ms(data, "wav"))
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Edge 合成失败（voice=%s）: %s", binding.edge_voice, exc)
        return None


_TAG_RE = re.compile(r"^\s*\[")


def _tagged(text: str, emotion_tag: str) -> str:
    """Fish 的行内情绪标记（放在句首）；已有标记就不叠加。"""
    tag = str(emotion_tag or "").strip()
    if not tag or _TAG_RE.match(text or ""):
        return text
    if not tag.startswith("["):
        tag = f"[{tag}]"
    return f"{tag} {text}"


__all__ = [
    "FISH_DOWN_SECONDS",
    "SynthResult",
    "VoiceBinding",
    "binding_for_friend",
    "binding_for_persona",
    "edge_voice_valid",
    "fish_configured",
    "fish_status",
    "fish_usable",
    "load_cfg",
    "mark_fish_failure",
    "mark_fish_success",
    "probe",
    "reset_state",
    "synthesize",
    "valid_edge_voice",
]
