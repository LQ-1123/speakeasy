"""音频基础工具：PCM ↔ float32、重采样、wav 读写、静音裁剪、格式探测。

契约见 ``docs/CONTRACTS.md`` §1。硬性约束：

- **只依赖 numpy + 标准库**。不得 import ``scipy`` / ``librosa`` / ``soundfile`` /
  ``torchaudio``（它们要么没装，要么在 Windows 上装不上）。``soundfile`` 与
  ``ffmpeg`` 只作为**可选**解码路径在函数内部探测。
- 内存表示：PCM16 = ``bytes``（小端、mono、有符号 16 bit）；浮点 =
  ``np.ndarray[float32]``，取值范围 [-1.0, 1.0]。
- 采样率：外部输入固定 16 kHz，TTS 输出固定 24 kHz。

设计说明（重采样）：采用「抗混叠低通 FIR + 线性插值」两段式。降采样时先按目标
Nyquist 做窗函数 sinc 低通（Blackman 窗），再线性插值到目标长度；升采样时无需低通，
直接线性插值。16k↔24k 的线性插值误差在语音听感上可忽略，且不做多相滤波可以避免
引入巨大的算子表，纯 numpy 且完全可预测。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import wave
import math
from typing import Iterable

import numpy as np

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 外部音频输入采样率（浏览器已重采样）
SR_IN: int = 16000
#: TTS 输出采样率（CosyVoice2 / F5-TTS / tone 统一到 24k）
SR_OUT: int = 24000

_INT16 = np.dtype("<i2")  # 显式小端：Windows/浏览器解码依赖这个
_MAX_PCM16 = 32767.0


class AudioDecodeError(RuntimeError):
    """音频解码失败（文件缺失、格式不支持、缺少 ffmpeg 等）。"""


class AudioLimitError(AudioDecodeError):
    """Audio exceeds an explicit resource bound; never retry another decoder."""


def upload_limits() -> tuple[int, float, float]:
    """Configurable (encoded bytes, duration seconds, decode timeout seconds)."""
    def number(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
            return value if math.isfinite(value) and value > 0 else default
        except ValueError:
            return default

    return (int(number("SE_AUDIO_MAX_BYTES", 8 * 1024 * 1024)),
            number("SE_AUDIO_MAX_DURATION_S", 60),
            number("SE_AUDIO_DECODE_TIMEOUT_S", 10))


# --------------------------------------------------------------------------- #
# PCM16 <-> float32
# --------------------------------------------------------------------------- #
def pcm16_to_float32(pcm: bytes | bytearray | memoryview) -> np.ndarray:
    """PCM16 小端 mono → float32（除以 32768，shape=(-1,)）。"""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    buffer = bytes(pcm) if isinstance(pcm, memoryview) and not pcm.contiguous else pcm
    raw = np.frombuffer(buffer, dtype=_INT16)
    arr = raw.astype(np.float32)
    arr *= np.float32(1.0 / 32768.0)
    return arr


def float32_to_pcm16(x: np.ndarray) -> bytes:
    """float32 → PCM16 小端 mono bytes（先 clip 到 [-1, 1] 再量化）。

    NaN/Inf 会被清洗为 0 / 饱和值，避免把脏数据写进 wav 或发给浏览器。
    """
    arr = np.array(x, dtype=np.float32, copy=True).reshape(-1)
    if arr.size == 0:
        return b""
    np.nan_to_num(arr, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
    np.clip(arr, -1.0, 1.0, out=arr)
    arr *= np.float32(_MAX_PCM16)
    np.rint(arr, out=arr)
    np.clip(arr, -32768.0, 32767.0, out=arr)
    return arr.astype(_INT16).tobytes()


def pcm16_samples(pcm: bytes) -> int:
    """PCM16 字节串包含的采样点数。"""
    return len(pcm) // 2


def concat_pcm16(parts: Iterable[bytes]) -> bytes:
    """拼接多段 PCM16（保持顺序）。"""
    return b"".join(parts)


def ms_to_samples(ms: float, sr: int) -> int:
    """毫秒 → 采样点数（向下取整，至少 1）。"""
    if sr <= 0:
        return 0
    return max(0, int(round(float(ms) * sr / 1000.0)))


# --------------------------------------------------------------------------- #
# 重采样（纯 numpy）
# --------------------------------------------------------------------------- #
def _design_lowpass(numtaps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    """窗函数 sinc 低通 FIR（Blackman 窗），返回归一化到 DC 增益 1 的系数。"""
    numtaps = int(numtaps)
    if numtaps % 2 == 0:
        numtaps += 1
    numtaps = max(3, numtaps)
    n = np.arange(numtaps, dtype=np.float64) - (numtaps - 1) / 2.0
    fc = float(cutoff_hz) / float(fs)  # cycles / sample
    fc = max(1e-4, min(0.499, fc))
    h = 2.0 * fc * np.sinc(2.0 * fc * n)
    h *= np.blackman(numtaps)
    s = float(h.sum())
    if s == 0.0:
        return np.zeros(numtaps, dtype=np.float32)
    return (h / s).astype(np.float32)


def _lowpass(x: np.ndarray, cutoff_hz: float, fs: float) -> np.ndarray:
    if x.size < 3:
        return x
    # 抽头数跟 fs 走：16k → 101，24k → 151；再夹到信号长度以内
    numtaps = int(round(float(fs) / 160.0))
    numtaps = max(11, numtaps | 1)
    if numtaps >= x.size * 2:
        numtaps = max(11, (x.size // 2) | 1)
        if numtaps <= 3:
            return x
    h = _design_lowpass(numtaps, cutoff_hz, fs)
    return np.convolve(x, h, mode="same").astype(np.float32)


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """任意采样率转换（降采样先抗混叠低通，再线性插值）。

    Args:
        x: 单声道 float32 波形。
        sr_in: 输入采样率（>0）。
        sr_out: 输出采样率（>0）。

    Returns:
        新的 float32 数组；``sr_in == sr_out`` 时返回副本。
    """
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    if sr_in <= 0 or sr_out <= 0:
        raise ValueError(f"非法采样率: sr_in={sr_in}, sr_out={sr_out}")
    if sr_in == sr_out or arr.size == 0:
        return arr.copy()

    src = arr
    if sr_in > sr_out:
        # 目标 Nyquist 的 0.45 倍作为截止频率，留出过渡带
        src = _lowpass(src, cutoff_hz=0.45 * sr_out, fs=float(sr_in))

    n_out = int(round(src.size * float(sr_out) / float(sr_in)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    if n_out == 1:
        return np.array([src[0]], dtype=np.float32)

    pos = np.arange(n_out, dtype=np.float64) * (float(sr_in) / float(sr_out))
    i0 = np.floor(pos).astype(np.int64)
    frac = (pos - i0).astype(np.float32)
    i0 = np.clip(i0, 0, src.size - 1)
    i1 = np.clip(i0 + 1, 0, src.size - 1)
    out = src[i0] * (1.0 - frac) + src[i1] * frac
    return out.astype(np.float32)


# --------------------------------------------------------------------------- #
# 基础度量
# --------------------------------------------------------------------------- #
def duration_s(pcm: bytes, sr: int = SR_IN) -> float:
    """PCM16 时长（秒）。"""
    if sr <= 0:
        return 0.0
    return (len(pcm) / 2.0) / float(sr)


def array_duration_s(x: np.ndarray, sr: int) -> float:
    """float32 数组时长（秒）。"""
    if sr <= 0:
        return 0.0
    return float(np.asarray(x).size) / float(sr)


def rms(x: np.ndarray) -> float:
    """均方根能量（float32，[-1,1] 量纲）。空数组返回 0.0。"""
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(arr, dtype=np.float64))))


def peak(x: np.ndarray) -> float:
    """绝对值峰值。空数组返回 0.0。"""
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return 0.0
    return float(np.max(np.abs(arr)))


def normalize_peak(x: np.ndarray, target: float = 0.9) -> np.ndarray:
    """按峰值归一化到 ``target``（全静音时原样返回副本）。"""
    arr = np.array(x, dtype=np.float32, copy=True).reshape(-1)
    p = peak(arr)
    if p <= 1e-9:
        return arr
    return (arr * (float(target) / p)).astype(np.float32)


def trim_silence(
    x: np.ndarray,
    sr: int,
    threshold: float = 0.01,
    pad_ms: int = 100,
) -> np.ndarray:
    """裁掉首尾静音，两端各留 ``pad_ms`` 余量。

    全部低于阈值时返回**空数组**（调用方需自行处理"整段静音"的情况）。
    """
    arr = np.array(x, dtype=np.float32, copy=True).reshape(-1)
    if arr.size == 0:
        return arr
    loud = np.flatnonzero(np.abs(arr) >= float(threshold))
    if loud.size == 0:
        return np.zeros(0, dtype=np.float32)
    pad = ms_to_samples(pad_ms, sr)
    start = max(0, int(loud[0]) - pad)
    end = min(arr.size, int(loud[-1]) + 1 + pad)
    return arr[start:end].copy()


# --------------------------------------------------------------------------- #
# wav 读写
# --------------------------------------------------------------------------- #
def write_wav(path: str, x: np.ndarray, sr: int) -> None:
    """写 16-bit PCM mono wav（只用标准库 ``wave``）。"""
    sr = int(sr)
    if sr <= 0:
        raise ValueError(f"非法采样率: {sr}")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    pcm = float32_to_pcm16(x)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)


def _decode_pcm_bytes(raw: bytes, sampwidth: int) -> np.ndarray:
    """把 wav 的裸数据解码成 float32 [-1,1]。"""
    if sampwidth == 1:
        u = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        return (u - 128.0) / 128.0
    if sampwidth == 2:
        return np.frombuffer(raw, dtype=_INT16).astype(np.float32) / 32768.0
    if sampwidth == 3:
        b = np.frombuffer(raw, dtype=np.uint8)
        if b.size % 3:
            b = b[: b.size - (b.size % 3)]
        b = b.reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        return v.astype(np.float32) / 8388608.0
    if sampwidth == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise AudioDecodeError(f"不支持的 wav 采样位宽: {sampwidth * 8} bit")


def _parse_wav(fh: io.BufferedIOBase, max_duration_s: float | None = None) -> tuple[np.ndarray, int]:
    """解析 wav 文件对象 → (float32 mono, sample_rate)。"""
    try:
        with wave.open(fh, "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            comptype = wf.getcomptype()
            nframes = wf.getnframes()
            if max_duration_s is not None:
                if sr <= 0 or nframes > int(sr * max_duration_s):
                    raise AudioLimitError(f"音频时长超过 {max_duration_s:g}s duration limit")
                if nframes * nch * sw > upload_limits()[0]:
                    raise AudioLimitError("WAV decoded input exceeds audio bytes limit")
                if sr > 48000 or nch > 2:
                    # Unusual headers can make the numpy resampling FIR huge;
                    # use the subprocess time/output budget for those sources.
                    raise AudioDecodeError("WAV sample rate/channels require bounded ffmpeg")
            raw = wf.readframes(nframes)
    except wave.Error as exc:  # 例如 WAVE_FORMAT_IEEE_FLOAT / EXTENSIBLE
        raise AudioDecodeError(f"标准库 wave 无法解析: {exc}") from exc
    except EOFError as exc:
        raise AudioDecodeError(f"wav 数据被截断: {exc}") from exc

    if comptype != "NONE":
        raise AudioDecodeError(f"不支持的 wav 压缩类型: {comptype}")
    if sr <= 0:
        raise AudioDecodeError(f"wav 采样率非法: {sr}")
    x = _decode_pcm_bytes(raw, sw)
    if nch > 1:
        usable = (x.size // nch) * nch
        x = x[:usable].reshape(-1, nch).mean(axis=1)
    return x.astype(np.float32, copy=False), int(sr)


def _looks_like_wav(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return len(head) == 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    except OSError:
        return False


def _read_via_soundfile(path: str) -> tuple[np.ndarray, int] | None:
    """可选路径：soundfile 已安装时用它（支持 wav/flac/ogg）。"""
    try:
        import soundfile as sf  # noqa: PLC0415  (可选依赖，缺失即跳过)
    except Exception:
        return None
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception as exc:
        log.debug("soundfile 解码失败 %s: %s", path, exc)
        return None
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    return arr.reshape(-1), int(sr)


def _find_ffmpeg() -> str | None:
    for key in ("FFMPEG_BINARY", "FFMPEG_PATH"):
        val = os.environ.get(key)
        if val and os.path.isfile(val):
            return val
    return shutil.which("ffmpeg")


def _read_via_ffmpeg(path: str, target_sr: int, *, max_duration_s: float | None = None,
                     timeout_s: float = 10.0) -> tuple[np.ndarray, int]:
    """可选路径：用 ffmpeg 子进程把任意格式转成 wav 再解析（无 ffmpeg 则报错）。"""
    exe = _find_ffmpeg()
    if not exe:
        raise AudioDecodeError(
            "未找到 ffmpeg（PATH 与 FFMPEG_BINARY 均不可用），无法解码该格式；"
            "请安装 ffmpeg 或改用 wav"
        )
    if max_duration_s is not None:
        max_samples = int(target_sr * max_duration_s)
        max_bytes = (max_samples + 1) * 4
        # Read only bounded output after exit, rather than accumulating stdout in RAM.
        # A single extra sample distinguishes an overlong recording from an exact limit.
        cmd = [exe, "-nostdin", "-loglevel", "error", "-threads", "1", "-i", path,
               "-vn", "-ac", "1", "-ar", str(target_sr), "-threads", "1",
               "-t", str((max_samples + 1) / target_sr), "-fs", str(max_bytes),
               "-f", "f32le", "-acodec", "pcm_f32le", "-"]
        try:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                proc = subprocess.run(cmd, check=False, timeout=timeout_s,
                                      stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
                output.seek(0)
                raw = output.read(max_bytes + 4)
                if len(raw) > max_samples * 4:
                    raise AudioLimitError(f"音频时长超过 {max_duration_s:g}s duration limit")
                if proc.returncode != 0 or not raw:
                    errors.seek(0)
                    detail = errors.read(2000).decode("utf-8", "replace")
                    raise AudioDecodeError(f"ffmpeg 解码失败（code={proc.returncode}）: {detail}")
        except subprocess.TimeoutExpired as exc:
            raise AudioDecodeError(f"ffmpeg 解码超时（{timeout_s:g}s）") from exc
        except OSError as exc:
            raise AudioDecodeError(f"无法启动 ffmpeg: {exc}") from exc
        return np.frombuffer(raw, dtype="<f4"), target_sr

    cmd = [
        exe, "-nostdin", "-loglevel", "error",
        "-threads", "1", "-i", path, "-threads", "1",
        "-vn", "-ac", "1", "-ar", str(int(target_sr)),
        "-f", "wav", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, check=False, timeout=timeout_s,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioDecodeError(f"ffmpeg 解码超时（{timeout_s:g}s）: {path}") from exc
    except OSError as exc:
        raise AudioDecodeError(f"无法启动 ffmpeg: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AudioDecodeError(
            f"ffmpeg 解码失败（code={proc.returncode}）: {' | '.join(err[-3:]) or '无输出'}"
        )
    x, sr = _parse_wav(io.BytesIO(proc.stdout))
    return x, sr


def _finalize(data: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    arr = arr.reshape(-1).copy()
    if not np.all(np.isfinite(arr)):
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
    np.clip(arr, -1.0, 1.0, out=arr)
    if int(sr) != int(target_sr) and int(sr) > 0:
        arr = resample(arr, int(sr), int(target_sr))
    return arr.astype(np.float32, copy=False)


def read_audio_file(path: str, target_sr: int = SR_IN, *, max_duration_s: float | None = None,
                    timeout_s: float = 10.0) -> np.ndarray:
    """读音频文件 → float32 mono @ ``target_sr``。

    解码顺序：标准库 ``wave``（wav）→ ``soundfile``（若已安装）→ ``ffmpeg`` 子进程。
    全部失败抛 :class:`AudioDecodeError`（错误信息包含每条路径的失败原因）。
    """
    if not path:
        raise AudioDecodeError("音频路径为空")
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise AudioDecodeError(f"音频文件不存在: {path}")
    target_sr = int(target_sr)
    if target_sr <= 0:
        raise ValueError(f"非法目标采样率: {target_sr}")

    ext = os.path.splitext(path)[1].lower()
    problems: list[str] = []

    if ext in (".wav", ".wave", "") or _looks_like_wav(path):
        try:
            with open(path, "rb") as fh:
                data, sr = _parse_wav(fh, max_duration_s=max_duration_s)
            return _finalize(data, sr, target_sr)
        except AudioLimitError:
            raise
        except AudioDecodeError as exc:
            problems.append(f"wave: {exc}")
        except OSError as exc:
            problems.append(f"wave: {exc}")

    # The optional soundfile convenience path decodes the whole source. Bounded
    # untrusted uploads use ffmpeg's duration/output cap instead.
    got = _read_via_soundfile(path) if max_duration_s is None else None
    if got is not None:
        return _finalize(got[0], got[1], target_sr)

    try:
        data, sr = _read_via_ffmpeg(path, target_sr, max_duration_s=max_duration_s,
                                  timeout_s=timeout_s)
        return _finalize(data, sr, target_sr)
    except AudioLimitError:
        raise
    except AudioDecodeError as exc:
        problems.append(f"ffmpeg: {exc}")

    raise AudioDecodeError(f"无法解码音频 {path}: " + "; ".join(problems))


def probe_audio_file(path: str) -> tuple[int, float]:
    """探测音频的 (sample_rate, duration_s)，**不解码整段**（能省则省）。

    用于参考音频体检（``ReferenceLibrary.validate()``）。顺序：
    wav 头 → soundfile.info → ffprobe → 最后才整段解码兜底。
    """
    if not path or not os.path.isfile(path):
        raise AudioDecodeError(f"音频文件不存在: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in (".wav", ".wave", "") or _looks_like_wav(path):
        try:
            with wave.open(path, "rb") as wf:
                sr = wf.getframerate()
                n = wf.getnframes()
                if sr > 0:
                    return int(sr), float(n) / float(sr)
        except (wave.Error, OSError, EOFError) as exc:
            log.debug("probe wave 失败 %s: %s", path, exc)

    try:
        import soundfile as sf  # noqa: PLC0415  (可选依赖)

        info = sf.info(path)
        if info.samplerate and info.frames >= 0:
            return int(info.samplerate), float(info.frames) / float(info.samplerate)
    except Exception as exc:  # noqa: BLE001 - 可选路径，任何失败都继续
        log.debug("probe soundfile 失败 %s: %s", path, exc)

    exe = shutil.which(os.environ.get("FFPROBE_BINARY", "ffprobe"))
    if exe:
        cmd = [
            exe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,duration",
            "-of", "default=noprint_wrappers=1", path,
        ]
        try:
            proc = subprocess.run(
                cmd, check=False, timeout=30,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            info: dict[str, str] = {}
            for line in proc.stdout.decode("utf-8", "replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            sr = int(float(info.get("sample_rate", "0") or 0))
            dur = float(info.get("duration", "0") or 0)
            if sr > 0 and dur > 0:
                return sr, dur
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            log.debug("probe ffprobe 失败 %s: %s", path, exc)

    x = read_audio_file(path, target_sr=SR_IN)
    return SR_IN, array_duration_s(x, SR_IN)


__all__ = [
    "SR_IN",
    "SR_OUT",
    "AudioDecodeError",
    "AudioLimitError",
    "upload_limits",
    "pcm16_to_float32",
    "float32_to_pcm16",
    "pcm16_samples",
    "concat_pcm16",
    "ms_to_samples",
    "resample",
    "duration_s",
    "array_duration_s",
    "rms",
    "peak",
    "normalize_peak",
    "trim_silence",
    "write_wav",
    "read_audio_file",
    "probe_audio_file",
]
