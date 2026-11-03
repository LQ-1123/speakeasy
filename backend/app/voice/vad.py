"""流式 VAD：Silero-VAD v5 优先，缺失时降级为自适应能量 VAD。

契约见 ``docs/CONTRACTS.md`` §2。

状态机（两个后端共用，保证接口行为一致）::

    IDLE ──首帧判定为语音──▶ CANDIDATE ──语音累计≥min_speech_ms──▶ SPEAKING
      ▲                        │                                      │
      │                        └──静音≥min_silence_ms（判为噪声）──────┘
      │                                                               │
      └──────────── SPEECH_END（静音≥min_silence_ms / 超时 / flush）───┘

为什么需要 ``CANDIDATE`` 而不是"先报 SPEECH_START 再回滚"：
回滚会让上层已经发出的 ``speech_start`` 变成谎言（前端可能已经切 UI 状态）。
延迟 ``min_speech_ms``（默认 200 ms）再报 start，代价可接受——barge-in 打自由
前端本地能量检测负责（见 ``docs/PROTOCOL.md`` §2.4），不依赖这里。

**preroll**（``speech_pad_ms``）：IDLE 期间持续把最近的音频块存进环形缓冲，
一旦进入 CANDIDATE 就把这些"触发点之前的音频"并入 utterance，否则每个词的首
辅音都会被吃掉（典型 bug）。尾音同理：SPEECH_END 时只保留最后一个语音块之后
``speech_pad_ms`` 的尾巴，而不是把整段 ``min_silence_ms`` 静音都塞进 utterance。

异步/线程约束：``push()`` 是**同步**函数，由 WS 接收循环调用；silero 的模型加载
在第一次 ``push()`` 时懒执行（构造时不加载权重，见契约 §0）。
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from app.config import VADSettings
from app.logging_utils import get_logger
from app.voice.audio_utils import SR_IN, float32_to_pcm16, ms_to_samples, pcm16_to_float32

log = get_logger(__name__)

#: silero v5 要求的块长（samples @16k）
_DEFAULT_WINDOW = 512

_STATE_IDLE = 0
_STATE_CANDIDATE = 1
_STATE_SPEAKING = 2


class VADEvent(str, Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass
class VADResult:
    """一次 ``push()`` / ``flush()`` 的产出。"""

    event: VADEvent | None = None
    utterance: bytes | None = None
    """仅当 ``event == SPEECH_END`` 时为完整一句话 PCM16@16k（含 preroll 与尾音 pad）。"""
    speech_ms: int = 0
    """当前/刚结束语音段的时长（毫秒，按"判定为语音的块"累计）。"""

    def __bool__(self) -> bool:
        return self.event is not None or self.utterance is not None


def _silence_result(speech_ms: int = 0) -> VADResult:
    return VADResult(event=None, utterance=None, speech_ms=int(speech_ms))


class StreamingVAD:
    """流式语音活动检测（16 kHz / mono / PCM16 小端）。

    Args:
        cfg: ``app.config.VADSettings``；``None`` 时用默认值（**不**读全局 settings，
            便于单测注入）。

    Example::

        vad = StreamingVAD()
        for frame in frames:                      # 任意长度，20~40ms 常见
            r = vad.push(frame)
            if r.event is VADEvent.SPEECH_END:
                await stt.transcribe(r.utterance)
    """

    def __init__(self, cfg: VADSettings | None = None) -> None:
        self.cfg = cfg or VADSettings()
        self._sr = SR_IN
        self._window = int(self.cfg.window_size_samples or _DEFAULT_WINDOW)
        if self._window <= 0:
            self._window = _DEFAULT_WINDOW
        self._block_ms = self._window * 1000.0 / self._sr

        # ---- 可配置阈值 ----
        self._threshold = float(self.cfg.threshold)
        self._min_silence_ms = float(self.cfg.min_silence_ms)
        self._min_speech_ms = float(self.cfg.min_speech_ms)
        self._pad_ms = max(0.0, float(self.cfg.speech_pad_ms))
        self._max_utterance_ms = max(0.05, float(self.cfg.max_utterance_s)) * 1000.0
        self._pad_samples = ms_to_samples(self._pad_ms, self._sr)
        self._max_samples = ms_to_samples(self._max_utterance_ms, self._sr)

        # ---- 能量后端参数 ----
        self._energy_threshold = float(self.cfg.energy_threshold)
        self._energy_abs_floor = max(0.0, float(self.cfg.energy_abs_floor))
        self._noise_floor = 0.0
        self._noise_seen = 0
        self._energy_speech_scale = 1.0     # 由校准阶段微调（预留）
        self._stay_ratio = 0.6              # 迟滞：滞回阈值 = 进入阈值 * 0.6
        self._noise_alpha = 0.05

        # ---- 后端选择 ----
        self._requested_backend = str(self.cfg.backend or "auto")
        self._backend: str | None = None    # 懒解析：None 表示尚未探测
        self._silero: Any = None
        self._silero_failed = False
        self._last_prob: float | None = None

        # ---- 流式状态 ----
        self._pending = np.zeros(0, dtype=np.float32)   # 未凑满一个 window 的余量
        self._preroll: deque[np.ndarray] = deque()
        self._preroll_n = 0
        self._buf: list[np.ndarray] = []
        self._buf_n = 0
        self._state = _STATE_IDLE
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._last_speech_end = 0           # 最后一个语音块结束处的 sample 偏移
        self._outbox: deque[VADResult] = deque()
        self._events_emitted = 0

    # ------------------------------------------------------------------ #
    # 公开属性
    # ------------------------------------------------------------------ #
    @property
    def is_speaking(self) -> bool:
        """是否处于**确认的**说话状态（``CANDIDATE`` 不算）。"""
        return self._state == _STATE_SPEAKING

    @property
    def is_active(self) -> bool:
        """是否处于任何活跃段（候选或确认说话）。"""
        return self._state != _STATE_IDLE

    @property
    def backend_name(self) -> str:
        """实际使用的后端（首次 probe 前返回 ``"auto"`` 之类的待定名）。"""
        if self._backend is None:
            return "pending"
        return self._backend

    @property
    def last_probability(self) -> float | None:
        """最近一个块的语音概率（silero）或归一化能量（energy），供调试。"""
        return self._last_prob

    # ------------------------------------------------------------------ #
    # 后端探测
    # ------------------------------------------------------------------ #
    def warmup(self) -> str:
        """显式探测后端（同步）。幂等，可在启动时调用以免首帧卡顿。"""
        self._ensure_backend()
        return str(self._backend)

    def _ensure_backend(self) -> None:
        if self._backend is not None:
            return
        want = self._requested_backend
        if want in ("silero", "auto"):
            if self._init_silero():
                self._backend = "silero"
                log.info("VAD backend = silero-vad (window=%d @%dHz)", self._window, self._sr)
                return
            if want == "silero":
                log.warning("VAD backend=silero 但加载失败，降级为 energy（行为一致，精度略低）")
        elif want == "mock":
            log.info("VAD backend=mock → 使用 energy 实现（接口一致）")
        self._backend = "energy"
        log.info(
            "VAD backend = energy (thr=%.4f, abs_floor=%.4f, window=%d @%dHz)",
            self._energy_threshold, self._energy_abs_floor, self._window, self._sr,
        )

    def _init_silero(self) -> bool:
        """尝试加载 silero-vad。返回是否可用。**不抛异常**。"""
        if self._silero_failed:
            return False
        try:
            from silero_vad import load_silero_vad  # noqa: PLC0415 - 重依赖，函数内 import
        except Exception as exc:  # ImportError 或依赖其内部的 torch 缺失
            log.info("silero-vad 不可用（%s: %s），使用 energy 后端", type(exc).__name__, exc)
            self._silero_failed = True
            return False

        model = None
        # 路径 1：官方 v5 loader
        try:
            model = load_silero_vad()
        except Exception as exc:  # noqa: BLE001 - 版本差异，继续尝试旧路径
            log.debug("load_silero_vad() 失败: %s", exc)

        # 路径 2：torch.hub（旧版 / 无包时）
        if model is None:
            try:
                import torch  # noqa: PLC0415

                model, _ = torch.hub.load(
                    repo_or_dir="snakers4/silero-vad",
                    model="silero_vad",
                    trust_repo=True,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("silero-vad 加载失败（%s: %s），使用 energy 后端", type(exc).__name__, exc)
                self._silero_failed = True
                return False

        try:
            model.eval()
        except Exception:  # pragma: no cover - 部分实现无 eval
            pass
        self._silero = model
        return True

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """清空全部流式状态（切换会话 / 打断后调用）。"""
        self._pending = np.zeros(0, dtype=np.float32)
        self._preroll.clear()
        self._preroll_n = 0
        self._buf = []
        self._buf_n = 0
        self._state = _STATE_IDLE
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._last_speech_end = 0
        self._outbox.clear()
        self._last_prob = None
        self._reset_silero_states()

    def push(self, pcm16_frame: bytes) -> VADResult:
        """喂一帧 PCM16@16k（任意长度），返回本帧触发的**第一个**事件。

        一帧内若同时发生多个事件（极少见），其余事件排队，由后续 ``push()``
        或 ``flush()`` 依次取出——保证事件不丢失、不乱序。
        """
        if self._outbox:
            return self._outbox.popleft()

        self._ensure_backend()

        if not pcm16_frame:
            return _silence_result(self._current_speech_ms())

        if len(pcm16_frame) % 2:                      # 半个采样：丢掉尾字节
            pcm16_frame = pcm16_frame[:-1]
        samples = pcm16_to_float32(pcm16_frame)
        if samples.size == 0:
            return _silence_result(self._current_speech_ms())

        if self._pending.size:
            samples = np.concatenate((self._pending, samples))
            self._pending = np.zeros(0, dtype=np.float32)

        w = self._window
        n_full = samples.size // w
        events: list[VADResult] = []
        for i in range(n_full):
            block = samples[i * w:(i + 1) * w]
            ev = self._process_block(block)
            if ev is not None:
                events.append(ev)

        rest = samples[n_full * w:]
        # 余量最多保留一个 window（极端长帧也不会无限增长）
        self._pending = rest[-w:].copy() if rest.size > w else rest.copy()

        if not events:
            return _silence_result(self._current_speech_ms())
        self._outbox.extend(events[1:])
        return events[0]

    def flush(self) -> VADResult:
        """手动结束（用户点"停止"）。

        正在说话 → 强制产出 utterance（``SPEECH_END``）；
        处于候选段但已累计到一半以上 ``min_speech_ms`` → 也产出（用户主动结束，
        不该按噪声丢弃）；
        其余情况返回空结果并复位。
        """
        if self._outbox:
            return self._outbox.popleft()

        if self._state == _STATE_SPEAKING:
            return self._finalize()

        if self._state == _STATE_CANDIDATE:
            flush_min = min(self._min_speech_ms, 120.0)
            if self._speech_ms >= flush_min:
                log.debug("flush(): candidate 段 %.0fms ≥ %.0fms，按主动结束产出", self._speech_ms, flush_min)
                return self._finalize()
            log.debug("flush(): candidate 段仅 %.0fms，判为噪声丢弃", self._speech_ms)

        speech_ms = self._current_speech_ms()
        self._discard_segment()
        return _silence_result(speech_ms)

    # ------------------------------------------------------------------ #
    # 内部：逐块处理
    # ------------------------------------------------------------------ #
    def _process_block(self, block: np.ndarray) -> VADResult | None:
        active = self._state != _STATE_IDLE
        is_speech = self._classify(block, active)
        block_ms = self._block_ms

        if self._state == _STATE_IDLE:
            self._preroll_append(block)
            if not is_speech:
                return None
            # 触发：把 preroll（含本块）整体并入 utterance
            self._state = _STATE_CANDIDATE
            self._buf = list(self._preroll)
            self._buf_n = self._preroll_n
            self._preroll.clear()
            self._preroll_n = 0
            self._speech_ms = block_ms
            self._silence_ms = 0.0
            self._last_speech_end = self._buf_n
            if self._min_speech_ms <= 0:
                # 不需要确认：直接进 SPEAKING
                self._state = _STATE_SPEAKING
                return VADResult(
                    event=VADEvent.SPEECH_START, utterance=None,
                    speech_ms=int(round(self._speech_ms)),
                )
            return None

        # CANDIDATE / SPEAKING：继续累积
        self._buf.append(block)
        self._buf_n += block.size
        if is_speech:
            self._speech_ms += block_ms
            self._silence_ms = 0.0
            self._last_speech_end = self._buf_n
        else:
            self._silence_ms += block_ms

        total_ms = self._buf_n * 1000.0 / self._sr

        if self._state == _STATE_CANDIDATE:
            if self._speech_ms >= self._min_speech_ms:
                self._state = _STATE_SPEAKING
                return VADResult(
                    event=VADEvent.SPEECH_START, utterance=None,
                    speech_ms=int(round(self._speech_ms)),
                )
            if self._silence_ms >= self._min_silence_ms:
                log.debug("VAD: 候选段被丢弃（语音 %.0fms，静音 %.0fms）", self._speech_ms, self._silence_ms)
                self._discard_segment()
                return None
            if total_ms >= self._max_utterance_ms:      # 兜底：不会无限累积
                self._discard_segment()
                return None
            return None

        # SPEAKING
        if self._silence_ms >= self._min_silence_ms:
            return self._finalize()
        if total_ms >= self._max_utterance_ms:
            log.info("VAD: 达到 max_utterance_s=%.1fs，强制截断", self.cfg.max_utterance_s)
            return self._finalize(force=True)
        return None

    # ------------------------------------------------------------------ #
    # 内部：分类
    # ------------------------------------------------------------------ #
    def _classify(self, block: np.ndarray, active: bool) -> bool:
        if self._backend == "silero":
            prob = self._silero_prob(block)
            if prob is None:
                # 运行期失败 → 永久降级，保证本流不中断
                return self._energy_classify(block, active)
            self._last_prob = float(prob)
            if active:
                # 迟滞：已在段内时用更低的阈值，避免词间弱音把段切断
                return prob >= max(0.10, self._threshold * 0.45)
            return prob >= self._threshold
        return self._energy_classify(block, active)

    def _silero_prob(self, block: np.ndarray) -> float | None:
        """跑一次 silero，返回语音概率；失败返回 None 并降级到 energy。"""
        model = self._silero
        if model is None:
            self._fallback_to_energy("silero 模型为空")
            return None
        try:
            import torch  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - 理论上不会发生
            self._fallback_to_energy(f"torch 不可用: {exc}")
            return None
        try:
            with torch.inference_mode():
                t = torch.from_numpy(np.ascontiguousarray(block, dtype=np.float32))
                out = None
                # v5: model(x, sr) 其中 x 形状 (1, N) 或 (N,)
                for shape in ((1, t.numel()), (t.numel(),)):
                    try:
                        out = model(t.reshape(shape), self._sr)
                        break
                    except (RuntimeError, TypeError, ValueError) as exc:
                        last_err = exc
                if out is None:
                    raise RuntimeError(f"无法匹配 silero 调用签名: {last_err}")
            arr = out.detach().to("cpu").numpy() if hasattr(out, "detach") else np.asarray(out)
            return float(np.asarray(arr).reshape(-1)[0])
        except Exception as exc:  # noqa: BLE001 - 任何推理失败都降级
            self._fallback_to_energy(f"silero 推理失败: {type(exc).__name__}: {exc}")
            return None

    def _fallback_to_energy(self, reason: str) -> None:
        if self._backend != "silero":
            return
        log.warning("VAD 运行期降级到 energy 后端（%s）", reason)
        self._backend = "energy"
        self._silero_failed = True
        self._silero = None

    def _energy_classify(self, block: np.ndarray, active: bool) -> bool:
        """自适应噪声底 + RMS/峰值双阈值 + 迟滞。"""
        arr = np.asarray(block, dtype=np.float32)
        if arr.size == 0:
            return False
        sq = float(np.mean(np.square(arr, dtype=np.float64)))
        e = float(np.sqrt(sq))
        p = float(np.max(np.abs(arr))) if arr.size else 0.0
        self._last_prob = e

        if self._noise_seen < 8:
            # 前几块建立噪声底种子，但要**防止把语音本身当成噪声底**：
            # 若新块的能量明显高于当前估计（且高于绝对阈值），说明用户一上来就说话了，
            # 此时只允许噪声底缓慢上升，否则 enter 会被顶到天花板，整段语音都检测不到。
            if self._noise_seen == 0:
                self._noise_floor = min(e, self._energy_threshold)
            elif e > self._noise_floor * 2.5:
                self._noise_floor = 0.9 * self._noise_floor + 0.1 * e
            else:
                self._noise_floor = 0.5 * self._noise_floor + 0.5 * e
            self._noise_seen += 1
        enter = max(
            self._energy_threshold,
            self._noise_floor * 3.0,
            self._energy_abs_floor,
            1e-4,
        ) * self._energy_speech_scale
        thr = enter * self._stay_ratio if active else enter
        peak_thr = max(thr * 1.2, 2e-4)
        hit = (e > thr) and (p > peak_thr)

        if not active and not hit:
            # 只在空闲且非语音时跟踪噪声底（慢速 EMA）
            a = self._noise_alpha
            self._noise_floor = (1.0 - a) * self._noise_floor + a * e
        return hit

    # ------------------------------------------------------------------ #
    # 内部：缓冲与收尾
    # ------------------------------------------------------------------ #
    def _preroll_append(self, block: np.ndarray) -> None:
        if self._pad_samples <= 0:
            return
        self._preroll.append(block)
        self._preroll_n += block.size
        # 环形缓冲：只保留 speech_pad_ms 对应的样本
        while self._preroll_n - self._preroll[0].size >= self._pad_samples and len(self._preroll) > 1:
            old = self._preroll.popleft()
            self._preroll_n -= old.size

    def _finalize(self, force: bool = False) -> VADResult:
        """收尾当前段并产出 ``SPEECH_END``。"""
        # 必须在 _discard_segment() 之前把统计量存到局部变量：
        # _discard_segment() 会把 self._speech_ms 清零，之后再读就恒为 0，
        # 会导致每一段语音都被误判为"短于 min_speech_ms"而丢弃。
        speech_ms = int(round(self._speech_ms))
        if not self._buf:
            self._discard_segment()
            return _silence_result(speech_ms)

        total = np.concatenate(self._buf) if len(self._buf) > 1 else self._buf[0]
        end = total.size
        # 尾音 pad：只保留"最后一个语音块 + speech_pad_ms"
        keep = min(end, self._last_speech_end + self._pad_samples)
        # 前导 pad：preroll 已经在前面，无需再补
        keep = max(0, min(keep, self._max_samples if force else end))
        if not force:
            keep = min(keep, self._max_samples)
        utter = total[:keep] if keep < end else total

        min_samples = ms_to_samples(self._min_speech_ms, self._sr)
        trimmed = np.zeros(0, dtype=np.float32)
        if utter.size:
            trimmed = _trim_edges(utter, self._sr, threshold=0.004, pad_ms=int(self._pad_ms))

        # 用局部 speech_ms（不是 self._speech_ms）做判定，详见函数开头注释
        too_short = speech_ms < self._min_speech_ms

        self._discard_segment()

        if too_short and not force:
            log.debug("VAD: 语音 %dms < min_speech_ms %.0fms，丢弃", speech_ms, self._min_speech_ms)
            return _silence_result(speech_ms)
        if utter.size < min_samples and not force:
            log.debug("VAD: 样本不足 min_speech_ms，丢弃")
            return _silence_result(speech_ms)

        pcm = float32_to_pcm16(utter)
        if not pcm:                                    # 理论上不会发生
            return _silence_result(speech_ms)
        self._events_emitted += 1
        log.debug(
            "VAD: SPEECH_END utterance=%.2fs speech=%.0fms (total_events=%d)",
            utter.size / self._sr, speech_ms, self._events_emitted,
        )
        return VADResult(event=VADEvent.SPEECH_END, utterance=pcm, speech_ms=speech_ms)

    def _discard_segment(self) -> None:
        """丢弃当前段，回到 IDLE（**不**动噪声底）。"""
        self._buf = []
        self._buf_n = 0
        self._state = _STATE_IDLE
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._last_speech_end = 0
        self._preroll.clear()
        self._preroll_n = 0
        self._reset_silero_states()

    def _current_speech_ms(self) -> int:
        return int(round(self._speech_ms)) if self._state != _STATE_IDLE else 0

    def _reset_silero_states(self) -> None:
        model = self._silero
        if model is None:
            return
        fn = getattr(model, "reset_states", None)
        if callable(fn):
            try:
                fn()
            except Exception as exc:  # pragma: no cover
                log.debug("silero reset_states 失败: %s", exc)


def _trim_edges(x: np.ndarray, sr: int, threshold: float, pad_ms: int) -> np.ndarray:
    """去掉 utterance 首尾残余的极低能量（preroll 里可能全是底噪）。

    与 ``audio_utils.trim_silence`` 的区别：这里阈值极低（默认 0.004），
    只清掉数字静音/底噪，不碰真实的气音与弱辅音。
    """
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr
    loud = np.flatnonzero(np.abs(arr) >= float(threshold))
    if loud.size == 0:
        return arr                                   # 全静音：不裁，交给上层判断
    pad = ms_to_samples(pad_ms, sr)
    start = max(0, int(loud[0]) - pad)
    end = min(arr.size, int(loud[-1]) + 1 + pad)
    if end <= start:
        return arr
    return arr[start:end].copy()


def backend_available(backend: str = "auto") -> dict[str, Any]:
    """探测 VAD 后端可用性（供 ``/api/health`` 使用，不加载模型权重）。"""
    info: dict[str, Any] = {"requested": backend, "silero": False}
    if backend in ("silero", "auto"):
        try:
            import importlib.util  # noqa: PLC0415

            info["silero"] = importlib.util.find_spec("silero_vad") is not None
            info["torch"] = importlib.util.find_spec("torch") is not None
        except Exception as exc:  # pragma: no cover
            info["error"] = str(exc)
    info["resolved"] = "silero" if info.get("silero") and info.get("torch") else "energy"
    return info


__all__ = [
    "VADEvent",
    "VADResult",
    "StreamingVAD",
    "backend_available",
]

if os.environ.get("SPEAKEASY_VAD_DEBUG"):  # pragma: no cover - 手工调试开关
    log.setLevel("DEBUG")
