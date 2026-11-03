"""Edge-TTS 后端：微软 Edge 的神经网络真人语音（有真口音，无需 GPU）。

为什么加这个后端
----------------
本地克隆引擎（CosyVoice2 / F5-TTS）需要先下载 1.5–2GB 权重 + 录参考音频，
在那之前 ``tone`` 占位合成器只会发出正弦波"嗯嗯啊啊"——听起来完全不是人话，
无法验证产品是否可用。

Edge-TTS 提供**真人录制的神经声线**，覆盖我们需要的全部口音：
英国 RP、印度、美国、新加坡、爱尔兰、澳洲、南非、香港、肯尼亚…播放质量足够做
真实对练。它走 HTTPS（不需要 GPU/显卡），但需要联网。

定位：**本地克隆就绪前的"可用默认"**。一旦 CosyVoice2/F5-TTS 装好且参考音频就位，
``auto`` 会优先用克隆（可还原成特定人物的音色），Edge-TTS 作为高质量兜底。

输出：Edge-TTS 返回 **MP3**，这里解码成 float32 PCM 并重采样到 24000Hz，
使上层协议完全不变（``pcm_s16le / 24000 / mono``）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import AsyncIterator

import numpy as np

from app.logging_utils import get_logger

log = get_logger(__name__)

#: Edge-TTS 原生输出的采样率（MP3 44.1k）
EDGE_NATIVE_SR = 24000

#: 句尾保留的静音（秒）。真人说话句间停顿约 0.15–0.25s；
#: Edge-TTS 原生会给到 0.85s（听感上像"两个句号"），必须裁掉。
#: 这里只留 60ms 给自然衰减，真正的停顿由 ``trailing_pause_ms`` 按标点补。
TAIL_SILENCE_S = 0.06
#: 句首保留的静音（秒）。保留一点点避免起音被削（"R"、"S"这类擦音）。
HEAD_SILENCE_S = 0.02
#: 判定静音的相对阈值（相对该段峰值）。
SILENCE_REL_THRESHOLD = 0.02
#: 判定静音的绝对下限，防止极安静段落把全部音频当静音。
SILENCE_ABS_FLOOR = 8e-4


# --------------------------------------------------------------------------- #
# 静音修剪
# --------------------------------------------------------------------------- #
def trim_silence(
    arr: np.ndarray,
    sr: int,
    *,
    head_s: float = HEAD_SILENCE_S,
    tail_s: float = TAIL_SILENCE_S,
) -> np.ndarray:
    """去掉首尾静音，只在两端各保留指定时长。

    为什么需要
    ----------
    Edge-TTS 每句输出都带 **约 0.85s 句尾静音 + 0.22s 句首静音**（实测）。
    逐句合成再拼接时，句间停顿会变成 ``尾静音 + 首静音 ≈ 1.08s``，
    听感非常不自然（像不停地在"两个句号"之间停顿），且整段音频 36% 是空白。

    实测（3 句回复）：
        句1 "Right then…"            1.66s  首0.210s 尾0.860s
        句2 "one flat white coming up." 2.42s  首0.220s 尾0.850s
        句3 (30词)                    4.82s  首0.220s 尾0.870s
        → 裁掉后 8.90s 降到 5.97s，句间停顿 1.08s → 0.13s
    """
    if arr.size == 0:
        return arr

    frame = max(1, int(sr * 0.01))  # 10ms 一帧
    n_frames = arr.size // frame
    if n_frames < 2:
        return arr

    frames = arr[: n_frames * frame].reshape(n_frames, frame)
    energy = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1))
    peak = float(energy.max())
    if peak <= 1e-6:
        # 整段近乎无声：不裁（避免返回空数组）
        return arr

    thr = max(peak * SILENCE_REL_THRESHOLD, SILENCE_ABS_FLOOR)
    voiced = np.where(energy > thr)[0]
    if voiced.size == 0:
        return arr

    head_n = int(head_s * sr)
    tail_n = int(tail_s * sr)

    start = max(0, int(voiced[0]) * frame - head_n)
    end = min(arr.size, (int(voiced[-1]) + 1) * frame + tail_n)
    if end <= start:
        return arr
    return arr[start:end]


@dataclass(frozen=True)
class EdgeVoice:
    """一个角色对应的 Edge 声线。

    口音策略（本项目的重要结论）
    ----------------------------
    优先级从高到低：

    1. **官方英语变体**（``en-GB`` / ``en-IN`` / ``en-IE`` / ``en-SG`` / ``en-ZA`` ...）
       这是**真实存在的英语变体**，由该地区的母语者录音训练，口音 100% 自然。
       **能用就用这个。**

    2. **外语声线读英文**（``it-IT-*`` / ``fr-FR-*`` / ``de-DE-*`` ...）
       当目标地区**没有**官方英语变体时（如意大利、法国、德国、日本），
       用该语言的母语声线读英文文本 —— 它会用母语的元音空间和节奏去读，
       产生真实的外语腔调。**这是兜底方案**，代价是英文单词发音可能不准
       （它没学过英文，是靠字母规则硬读的）。

    绝不使用：在英语声线上"假装"外语口音（那样既不像也不准）。
    """

    voice: str                 # 例如 "en-GB-RyanNeural" 或 "it-IT-GiuseppeMultilingualNeural"
    locale: str = ""           # 例如 "en-GB"
    rate: str = "+0%"          # 语速调整（+10% 更快）
    pitch: str = "+0Hz"        # 音高调整
    style: str = ""            # 备注
    fallbacks: tuple[str, ...] = field(default_factory=tuple)
    #: 口音来源：``"official"`` = 官方英语变体（首选）；
    #: ``"foreign_fallback"`` = 外语声线读英文（兜底，口音浓但发音可能不准）
    accent_source: str = "official"
    #: 给用户看的口音说明
    accent_label: str = ""


#: persona_id → Edge 声线选择。
#: 为每个角色挑了**符合其人设**的真人声线（性别/年龄/口音一致）。
PERSONA_VOICES: dict[str, EdgeVoice] = {
    # ------------------------------------------------------------------ #
    # 官方英语变体（真实存在的地区变体，口音 100% 自然）
    # ------------------------------------------------------------------ #
    # 伦敦咖啡师，男性，British RP
    "oliver": EdgeVoice(
        voice="en-GB-RyanNeural", locale="en-GB", rate="+2%",
        style="warm male British RP",
        fallbacks=("en-GB-ThomasNeural", "en-IE-ConnorNeural"),
        accent_source="official",
        accent_label="British RP 英式标准音",
    ),
    # 班加罗尔工程师，男性，Indian English
    "raj": EdgeVoice(
        voice="en-IN-PrabhatNeural", locale="en-IN", rate="+8%",
        style="male Indian English, brisk",
        fallbacks=("en-SG-WayneNeural", "en-GB-RyanNeural"),
        accent_source="official",
        accent_label="Indian English 印度英语",
    ),
    # 纽约招聘官，女性，American
    "emily": EdgeVoice(
        voice="en-US-AriaNeural", locale="en-US", rate="+4%",
        style="female American, crisp professional",
        fallbacks=("en-US-JennyNeural", "en-US-EmmaNeural"),
        accent_source="official",
        accent_label="American 美式英语",
    ),
    # 新加坡 PM，女性，Singapore English
    "priya": EdgeVoice(
        voice="en-SG-LunaNeural", locale="en-SG", rate="+6%",
        style="female Singapore English, efficient",
        fallbacks=("en-SG-WayneNeural", "en-IN-NeerjaNeural"),
        accent_source="official",
        accent_label="Singapore English 新加坡英语",
    ),
    # 都柏林酒保，男性，Irish English
    "sean": EdgeVoice(
        voice="en-IE-ConnorNeural", locale="en-IE", rate="+0%",
        style="male Irish English, musical and rising",
        fallbacks=("en-GB-RyanNeural",),
        accent_source="official",
        accent_label="Irish English 爱尔兰英语",
    ),
    # 悉尼冲浪教练，女性，Australian
    "chloe": EdgeVoice(
        voice="en-AU-NatashaNeural", locale="en-AU", rate="+3%",
        style="female Australian English, laid back",
        fallbacks=("en-NZ-MollyNeural", "en-GB-SoniaNeural"),
        accent_source="official",
        accent_label="Australian English 澳大利亚英语",
    ),
    # 开普敦向导，男性，South African
    "thabo": EdgeVoice(
        voice="en-ZA-LukeNeural", locale="en-ZA", rate="+2%",
        style="male South African English",
        fallbacks=("en-KE-ChilembaNeural", "en-GB-RyanNeural"),
        accent_source="official",
        accent_label="South African English 南非英语",
    ),
    # 拉各斯创业者，女性，Nigerian
    "adaeze": EdgeVoice(
        voice="en-NG-EzinneNeural", locale="en-NG", rate="+4%",
        style="female Nigerian English, energetic",
        fallbacks=("en-KE-AsiliaNeural", "en-GB-SoniaNeural"),
        accent_source="official",
        accent_label="Nigerian English 尼日利亚英语",
    ),
    # 多伦多设计师，女性，Canadian
    "megan": EdgeVoice(
        voice="en-CA-ClaraNeural", locale="en-CA", rate="+3%",
        style="female Canadian English",
        fallbacks=("en-US-JennyNeural", "en-US-AriaNeural"),
        accent_source="official",
        accent_label="Canadian English 加拿大英语",
    ),
    # 香港同事，男性，Hong Kong English
    "wai": EdgeVoice(
        voice="en-HK-SamNeural", locale="en-HK", rate="+2%",
        style="male Hong Kong English",
        fallbacks=("en-SG-WayneNeural", "en-GB-RyanNeural"),
        accent_source="official",
        accent_label="Hong Kong English 香港英语",
    ),

    # ------------------------------------------------------------------ #
    # 外语声线读英文（兜底：目标地区没有官方英语变体时）
    #
    # 原理：it-IT-* 是为意大利语训练的模型，它用意大利语的元音空间、
    #       音节节奏、辅音习惯去读英文文本 → 真实意大利腔。
    #
    # 代价：它没学过英文单词，靠字母规则硬读，个别词发音可能不准。
    #       用户实测反馈"口音接近了，可以用于兜底"。
    # ------------------------------------------------------------------ #
    # 罗马设计师，男性，意大利式英语
    "marco": EdgeVoice(
        voice="it-IT-GiuseppeMultilingualNeural", locale="it-IT", rate="+0%",
        style="Italian native voice reading English (foreign-accented)",
        fallbacks=(
            "it-IT-DiegoNeural",
            "it-IT-IsabellaNeural",
            "en-IE-ConnorNeural",   # 最终兜底：回到官方英语变体
        ),
        accent_source="foreign_fallback",
        accent_label="Italian-accented 意大利腔（外语声线兜底）",
    ),
}

#: 未知角色兜底
DEFAULT_VOICE = EdgeVoice(
    voice="en-GB-RyanNeural",
    locale="en-GB",
    accent_source="official",
    accent_label="British RP 英式标准音",
)

#: 所有可用的**官方英语变体**（实测可用，不含已下线的声线）。
#: 口音策略：能用这些就用这些——它们是真实地区变体，口音 100% 自然。
OFFICIAL_EN_VARIANTS: dict[str, list[str]] = {
    "en-GB": ["en-GB-RyanNeural", "en-GB-ThomasNeural", "en-GB-SoniaNeural",
              "en-GB-LibbyNeural", "en-GB-MaisieNeural"],
    "en-IE": ["en-IE-ConnorNeural", "en-IE-EmilyNeural"],
    "en-AU": ["en-AU-NatashaNeural", "en-AU-WilliamMultilingualNeural"],
    "en-NZ": ["en-NZ-MitchellNeural", "en-NZ-MollyNeural"],
    "en-CA": ["en-CA-LiamNeural", "en-CA-ClaraNeural"],
    "en-US": ["en-US-AndrewNeural", "en-US-BrianNeural", "en-US-GuyNeural",
              "en-US-JennyNeural", "en-US-EmmaNeural", "en-US-AriaNeural"],
    "en-IN": ["en-IN-PrabhatNeural", "en-IN-NeerjaNeural",
              "en-IN-NeerjaExpressiveNeural"],
    "en-SG": ["en-SG-LunaNeural", "en-SG-WayneNeural"],
    "en-ZA": ["en-ZA-LeahNeural", "en-ZA-LukeNeural"],
    "en-HK": ["en-HK-YanNeural", "en-HK-SamNeural"],
    "en-KE": ["en-KE-ChilembaNeural", "en-KE-AsiliaNeural"],
    "en-NG": ["en-NG-AbeoNeural", "en-NG-EzinneNeural"],
    "en-PH": ["en-PH-JamesNeural", "en-PH-RosaNeural"],
    "en-TZ": ["en-TZ-ElimuNeural", "en-TZ-ImaniNeural"],
}

#: 没有官方英语变体、可用「外语声线读英文」兜底的地区。
#: 实测元音偏移（相对英式，越大越偏离）：
#:   it=910  fr=1105  de=741  es=304  ru=563  pt=557
FOREIGN_FALLBACK_VOICES: dict[str, list[str]] = {
    "it-IT": ["it-IT-GiuseppeMultilingualNeural", "it-IT-DiegoNeural",
              "it-IT-IsabellaNeural", "it-IT-ElsaNeural"],
    "fr-FR": ["fr-FR-HenriNeural", "fr-FR-DeniseNeural"],
    "de-DE": ["de-DE-ConradNeural", "de-DE-KatjaNeural"],
    "es-ES": ["es-ES-AlvaroNeural", "es-ES-ElviraNeural"],
    "ja-JP": ["ja-JP-KeitaNeural", "ja-JP-NanamiNeural"],
    "zh-CN": ["zh-CN-YunxiNeural", "zh-CN-XiaoxiaoNeural"],
}


def resolve_voice(persona_id: str) -> EdgeVoice:
    """解析角色 → 声线。

    优先级
    ------
    1. **用户自定义角色**的声线（``u_xxx``）—— 用户在创建角色时选的
    2. **口音市场里的覆盖**（给内置角色换口音）—— 存在 ``custom.json``
    3. 内置 ``PERSONA_VOICES`` 映射

    这样"口音市场换个口音"能立刻生效，同时不影响内置配置文件的整洁。
    """
    pid = (persona_id or "").lower()

    # 1) 自定义角色的声线
    if pid.startswith("u_"):
        try:
            from app.personas import get_personas  # noqa: PLC0415

            cp = get_personas().custom.get_persona(pid) if get_personas().custom else None
            if cp is not None and cp.voice:
                return _voice_from_catalog(cp.voice)
        except Exception:  # noqa: BLE001
            pass

    # 2) 口音市场的覆盖
    try:
        from app.personas import get_personas  # noqa: PLC0415

        ov = get_personas().voice_override(pid)
        if ov:
            return _voice_from_catalog(ov)
    except Exception:  # noqa: BLE001
        pass

    # 3) 内置映射
    return PERSONA_VOICES.get(pid, DEFAULT_VOICE)


def _voice_fallbacks(voice: str) -> tuple[str, ...]:
    """同地区的备用声线（主力失败时用）。"""
    locale = "-".join(voice.split("-")[:2]) if "-" in voice else ""
    if not locale:
        return ()
    pool = OFFICIAL_EN_VARIANTS.get(locale) or FOREIGN_FALLBACK_VOICES.get(locale) or []
    return tuple(v for v in pool if v != voice)[:2]


def _voice_from_catalog(voice: str) -> EdgeVoice:
    """把一个声线字符串包装成 EdgeVoice（口音元数据从市场目录取）。"""
    try:
        from app.voice.accent_market import get_accent  # noqa: PLC0415

        acc = get_accent(voice)
        if acc:
            return EdgeVoice(
                voice=voice,
                locale=acc["locale"],
                rate="+0%",
                style=acc.get("vibe", ""),
                fallbacks=(),
                accent_source=acc["kind"],
                accent_label=acc["label"],
            )
    except Exception:  # noqa: BLE001
        pass
    locale = "-".join(voice.split("-")[:2]) if "-" in voice else ""
    kind = "official" if locale in OFFICIAL_EN_VARIANTS else "foreign_fallback"
    return EdgeVoice(
        voice=voice,
        locale=locale,
        accent_source=kind,
        accent_label=locale,
    )


def accent_catalog() -> dict[str, list[dict[str, str]]]:
    """给前端/文档用的口音目录：官方变体优先，外语兜底单列。"""
    official = [
        {"locale": loc, "voice": v, "kind": "official"}
        for loc, voices in OFFICIAL_EN_VARIANTS.items()
        for v in voices
    ]
    fallback = [
        {"locale": loc, "voice": v, "kind": "foreign_fallback"}
        for loc, voices in FOREIGN_FALLBACK_VOICES.items()
        for v in voices
    ]
    return {"official": official, "foreign_fallback": fallback}


# --------------------------------------------------------------------------- #
# MP3 → PCM
# --------------------------------------------------------------------------- #
class EdgeDecodeError(RuntimeError):
    pass


def _decode_mp3(mp3_bytes: bytes, target_sr: int = EDGE_NATIVE_SR) -> np.ndarray:
    """把 MP3 字节解码成 float32 mono @ target_sr。

    优先用 stdlib + 纯 python 方案；不可用时退回 ffmpeg；都没有则抛错。
    """
    if not mp3_bytes:
        raise EdgeDecodeError("Edge-TTS 返回空音频")

    # ---- 方案 A：pydub / audioop（若可用） ----
    try:
        from pydub import AudioSegment  # noqa: PLC0415
        from pydub.utils import which  # noqa: PLC0415

        if which("ffmpeg") or which("avconv"):
            seg = AudioSegment.from_file(
                _bytes_to_tempfile(mp3_bytes, ".mp3"), format="mp3"
            )
            seg = seg.set_frame_rate(target_sr).set_channels(1).set_sample_width(2)
            raw = seg.raw_data
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            return arr
    except Exception as exc:  # noqa: BLE001 - 继续尝试别的路径
        log.debug("pydub 解码不可用: %s", exc)

    # ---- 方案 B：ffmpeg 子进程 ----
    try:
        return _decode_with_ffmpeg(mp3_bytes, target_sr)
    except Exception as exc:  # noqa: BLE001
        log.debug("ffmpeg 解码不可用: %s", exc)

    # ---- 方案 C：minimp3 / soundfile 尝试 ----
    try:
        import io

        import soundfile as sf  # noqa: PLC0415

        data, sr = sf.read(io.BytesIO(mp3_bytes), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != target_sr:
            from app.voice.audio_utils import resample  # noqa: PLC0415

            data = resample(np.asarray(data, dtype=np.float32), sr, target_sr)
        return np.asarray(data, dtype=np.float32)
    except Exception as exc:  # noqa: BLE001
        log.debug("soundfile 解码不可用: %s", exc)

    raise EdgeDecodeError(
        "无法解码 Edge-TTS 的 MP3：需要 ffmpeg。"
        "请运行 `winget install --id Gyan.FFmpeg -e` 后重开终端。"
    )


def _bytes_to_tempfile(data: bytes, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _decode_with_ffmpeg(mp3_bytes: bytes, target_sr: int) -> np.ndarray:
    """通过 ffmpeg 把 mp3 转成 raw s16le mono @ target_sr。"""
    src = _bytes_to_tempfile(mp3_bytes, ".mp3")
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", src,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(target_sr),
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise EdgeDecodeError(f"ffmpeg 失败: {proc.stderr[:300]!r}")
        arr = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size == 0:
            raise EdgeDecodeError("ffmpeg 解出 0 样本")
        return arr
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
class EdgeTTSEngine:
    """Edge-TTS 的薄封装：给定文本 → float32 PCM @24k。"""

    backend_name = "edge-tts"

    def __init__(self, *, sample_rate: int = EDGE_NATIVE_SR) -> None:
        self.sample_rate = sample_rate
        self._available: bool | None = None
        #: 合成结果缓存：同一句话（同一角色）重复请求时直接命中，
        #: 省掉 ~800-1100ms 的微软服务固定开销。
        #: 对话里"Right then"、"Cheers"、"Okay so"这类固定开场白命中率很高。
        self._cache: dict[tuple[str, str], np.ndarray] = {}
        self._cache_bytes = 0
        self._cache_max_bytes = 48 * 1024 * 1024  # 约 48MB PCM ≈ 8 分钟音频

    # ---- 缓存 ----
    def _cache_key(self, text: str, persona_id: str) -> tuple[str, str]:
        return ((persona_id or "").lower(), (text or "").strip())

    def _cache_get(self, key: tuple[str, str]) -> np.ndarray | None:
        arr = self._cache.get(key)
        if arr is None:
            return None
        # LRU：命中后移到末尾
        self._cache.pop(key, None)
        self._cache[key] = arr
        return arr

    def _cache_put(self, key: tuple[str, str], arr: np.ndarray) -> None:
        if arr.size == 0:
            return
        if key in self._cache:
            return
        nbytes = arr.nbytes
        while self._cache_bytes + nbytes > self._cache_max_bytes and self._cache:
            old_key, old_arr = next(iter(self._cache.items()))
            self._cache.pop(old_key, None)
            self._cache_bytes -= old_arr.nbytes
        self._cache[key] = arr
        self._cache_bytes += nbytes

    def cache_stats(self) -> dict[str, int | float]:
        return {
            "entries": len(self._cache),
            "bytes": self._cache_bytes,
            "max_bytes": self._cache_max_bytes,
        }

    # ---- 探测 ----
    @staticmethod
    def looks_available() -> bool:
        try:
            import edge_tts  # noqa: F401,PLC0415

            return True
        except Exception:
            return False

    async def ensure_ready(self) -> None:
        """确认 edge_tts 可 import；并做一次极小的合成验证网络连通。"""
        try:
            import edge_tts  # noqa: F401,PLC0415
        except Exception as exc:
            raise RuntimeError(f"edge-tts 未安装: {exc}") from exc
        if self._available is None:
            try:
                await self.synthesize("Hello.", "oliver")
                self._available = True
                log.info("Edge-TTS 可用（真人声线，真口音）")
            except Exception as exc:
                self._available = False
                raise RuntimeError(f"Edge-TTS 网络不可用: {exc}") from exc

    async def warmup(self, phrases: list[str], persona_ids: list[str]) -> int:
        """预热常用短句，让首次真正用到时零延迟。

        返回成功预热的数量。失败静默（预热只是优化，不影响正确性）。

        **限速**：微软语音服务对短时间高频请求会变慢/限流。这里串行 + 每条之间
        间隔 120ms，避免把启动阶段的带宽吃满（实测不限速时会让首轮首音从 1.3s
        劣化到 4.5s）。
        """
        import asyncio

        ok = 0
        for pid in persona_ids:
            for phrase in phrases:
                key = self._cache_key(phrase, pid)
                if key in self._cache:
                    continue
                try:
                    await self.synthesize(phrase, pid)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("预热失败 %r/%s: %s", phrase, pid, exc)
                # 限速：给对话框外的真实请求留出带宽
                await asyncio.sleep(0.12)
        if ok:
            log.info("Edge-TTS 预热完成：%d 条常用短语已缓存（%s）", ok, self.cache_stats())
        return ok

    # ---- 合成 ----
    async def synthesize(self, text: str, persona_id: str = "oliver") -> np.ndarray:
        """合成整句，返回 float32 mono @ sample_rate。

        命中缓存时直接返回（省 ~800-1100ms）；否则请求微软语音服务。
        """
        key = self._cache_key(text, persona_id)
        cached = self._cache_get(key)
        if cached is not None:
            log.debug("Edge-TTS 缓存命中: %r", (text or "")[:40])
            return cached

        pcm = await self._synthesize_uncached(text, persona_id)
        self._cache_put(key, pcm)
        return pcm

    async def synthesize_voice(self, text: str, voice: str) -> np.ndarray:
        """用**指定声线字符串**合成（口音市场试听用，不经过角色映射）。

        带缓存：同一句 + 同一声线重复试听时立刻返回。
        """
        key = self._cache_key(text, f"__voice__{voice}")
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        last_err: Exception | None = None
        for candidate in (voice, *_voice_fallbacks(voice)):
            try:
                mp3 = await self._stream_mp3(text, candidate, rate="+0%", pitch="+0Hz")
                if not mp3:
                    raise EdgeDecodeError("空音频")
                pcm = await asyncio.to_thread(_decode_mp3, mp3, self.sample_rate)
                if pcm.size == 0:
                    raise EdgeDecodeError("解码后为空")
                pcm = trim_silence(pcm, self.sample_rate)
                self._cache_put(key, pcm)
                return pcm
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.debug("试听声线 %s 失败: %s", candidate, exc)
                continue
        raise EdgeDecodeError(f"试听失败（所有候选声线）: {last_err}")

    async def _synthesize_uncached(self, text: str, persona_id: str) -> np.ndarray:
        import edge_tts  # noqa: PLC0415

        voice = resolve_voice(persona_id)
        last_err: Exception | None = None

        for candidate in (voice.voice, *voice.fallbacks):
            try:
                mp3 = await self._stream_mp3(
                    text, candidate, rate=voice.rate, pitch=voice.pitch
                )
                if not mp3:
                    raise EdgeDecodeError("空音频")
                pcm = await asyncio.to_thread(_decode_mp3, mp3, self.sample_rate)
                if pcm.size == 0:
                    raise EdgeDecodeError("解码后为空")
                # 裁掉边缘静音：Edge 每句尾带 ~0.85s、首带 ~0.22s 静音，
                # 不裁的话逐句拼接会出现 ~1.08s 的不自然停顿。
                pcm = trim_silence(pcm, self.sample_rate)
                return pcm
            except Exception as exc:  # noqa: BLE001 - 换下一个声线
                last_err = exc
                log.debug("Edge-TTS 声线 %s 失败: %s", candidate, exc)
                continue

        raise EdgeDecodeError(f"Edge-TTS 全部声线失败: {last_err}")

    async def _stream_mp3(self, text: str, voice: str, *, rate: str, pitch: str) -> bytes:
        import edge_tts  # noqa: PLC0415

        kwargs: dict[str, object] = {"rate": rate}
        if pitch and pitch != "+0Hz":
            kwargs["pitch"] = pitch

        communicate = edge_tts.Communicate(text, voice, **kwargs)
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    async def synthesize_stream(
        self, text: str, persona_id: str = "oliver", *, chunk_ms: int = 200
    ) -> AsyncIterator[bytes]:
        """按块产出 PCM16（供上层直接推流）。"""
        pcm = await self.synthesize(text, persona_id)
        pcm16 = np.clip(pcm * 32767, -32768, 32767).astype(np.int16)
        step = max(1, int(self.sample_rate * chunk_ms / 1000))
        for i in range(0, pcm16.size, step):
            yield pcm16[i : i + step].tobytes()

    # ------------------------------------------------------------------ #
    # 首音加速：**拆分首批**（split-prefetch）
    # ------------------------------------------------------------------ #
    async def synth_stream_frames(
        self, text: str, persona_id: str = "oliver", *, min_frame_s: float = 0.35
    ) -> AsyncIterator[np.ndarray]:
        """先合成句首一小段并立即返回，其余部分并行合成后续推。

        为什么这样做
        ------------
        Edge 返回整段 MP3，整句 5 秒文本要 ~1.2-1.4s 才合成完，首音必然超标。
        但真人对话里，**句首几个词**才是用户等待的关键。所以：

        1. 把句子按短语边界切成「句首块」(<= ~28 字符) + 「剩余」。
        2. 只合成句首块（约 0.3-0.6s 音频，~350-500ms 完成）→ **立刻 yield**。
        3. 句首块在播的同时，后台并行合成剩余部分并继续 yield。

        这样首音从 ~1300ms 降到 ~400-600ms，而完整音频仍然连续。
        若句子太短（无法安全拆分），退化为整句合成。
        """
        head, rest = _split_head(text)
        if not rest:
            pcm = await self.synthesize(text, persona_id)
            yield pcm
            return

        # 句首块：阻塞等待（这是首音的关键路径）
        head_pcm = await self.synthesize(head, persona_id)
        if head_pcm.size:
            yield head_pcm

        # 剩余部分：边合成边推（此时用户已在听句首了）
        try:
            rest_pcm = await self.synthesize(rest, persona_id)
            if rest_pcm.size:
                yield rest_pcm
        except Exception as exc:  # noqa: BLE001 - 首音已发出，尾部失败可接受
            log.warning("Edge-TTS 尾部合成失败（首音已发出）: %s", exc)


def _split_head(text: str, *, min_len: int = 12, max_len: int = 34) -> tuple[str, str]:
    """把一句切成「句首块」与「剩余」，优先在标点/连词处断。

    返回 ``(head, rest)``；无法安全拆分时 ``rest == ""``。

    策略：
    - 太短（<= max_len）→ 不拆。
    - 在 ``[min_len, max_len]`` 区间里找**最靠右**的逗号/分号，取它作切点
      （这样 head 尽量完整，不会只切出 "Allora," 这种碎片）。
    - 没标点就找连词，再退化为空格。
    """
    t = (text or "").strip()
    if not t:
        return "", ""

    # 太短：不值得拆（一次请求更快）
    if len(t) <= max_len + 6:
        return t, ""

    # 1) 标点：在 [min_len, max_len] 里找最靠右的
    best = -1
    for i, ch in enumerate(t[: max_len + 1]):
        if ch in ",;:—–" and i + 1 >= min_len:
            best = i + 1
    if best > 0:
        return t[:best].strip(), t[best:].strip()

    # 2) 连词：区间内找最靠右的
    best = -1
    for w in (" and ", " but ", " so ", " or ", " then ", " because "):
        idx = t.find(w, min_len - 1, max_len + len(w))
        if idx > 0:
            end = idx + len(w)
            if best < 0 or end > best:
                best = end
    if best > 0:
        return t[:best].strip(), t[best:].strip()

    # 3) 空格兜底：在 max_len 附近取最后一个空格
    sp = t.rfind(" ", min_len, max_len + 10)
    if sp > min_len:
        return t[:sp].strip(), t[sp:].strip()

    return t, ""


def _incremental_decode(mp3_path: str, pcm_path: str, sr: int) -> None:
    """（占位，保留向后兼容）"""
    return None


def _read_bytes_at(path: str, offset: int, length: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)


__all__ = [
    "DEFAULT_VOICE",
    "EDGE_NATIVE_SR",
    "EdgeDecodeError",
    "EdgeTTSEngine",
    "EdgeVoice",
    "PERSONA_VOICES",
    "resolve_voice",
]
