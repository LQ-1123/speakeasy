"""V3 · 信息流朗读音频缓存（预生成，点击即播）。

为什么需要
----------
edge-TTS 合成一条动态需要 ~8 秒；用户点「朗读」再合成必然卡顿。
但信息流是**预生成语料**：内容固定、数量有限（当前 36 条），
完全可以在后台提前合成好，点击时直接读文件（毫秒级）。

设计
----
- 文件名 = ``{post_id}-{content_hash}-{voice}.wav``：内容/语言/声线任一变化，
  文件名就变 → 天然避免播放旧音频，也不需要手动失效。
- 启动后后台预热第一页（``WARM_STARTUP_ITEMS`` 条），信息流接口返回时
  顺带把本页动态放进合成队列；队列单并发（避免打爆 edge-TTS）。
- 点击时：文件存在 → 立即返回；不存在 → 同步合成并落盘（兜底）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

from app.logging_utils import get_logger
from app.v3.seed_data import detect_language, voice_for_language

log = get_logger(__name__)

#: 启动时预热多少条（第一页的可见内容）
WARM_STARTUP_ITEMS = 8

#: wav → mp3：体积约降 6-8 倍，传输/解码更快（有 ffmpeg 时启用）
MP3_BITRATE = "64k"
_FFMPEG = os.getenv("SE_FFMPEG") or shutil.which("ffmpeg") or ""


def _voice_for(post: dict, persona: dict) -> tuple[str, str]:
    """返回 (语言, 声线)：帖子的语言优先，人物声线只在同语言时复用。"""
    content = str(post.get("content") or "")
    persona_lang = str(persona.get("language") or "en").strip().lower()
    post_lang = (
        str(post.get("language") or "").strip().lower()
        or detect_language(content)
        or persona_lang
    )
    persona_voice = str(persona.get("voice_id") or "")
    if post_lang == persona_lang and persona_voice:
        return post_lang, persona_voice
    return post_lang, voice_for_language(
        post_lang, seed=str(persona.get("id") or ""), gender=str(persona.get("gender") or "")
    )


class PersonaAudioCache:
    """Persona 动态朗读音频的磁盘缓存 + 后台预热队列。"""

    def __init__(self, db: Any, voice_engine: Any, audio_dir: str) -> None:
        self.db = db
        self.voice = voice_engine
        self.audio_dir = audio_dir
        self.cache_dir = os.path.join(audio_dir, "v3")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._queue: asyncio.Queue[tuple[dict, dict]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._seen: set[str] = set()
        self.done = 0
        self.failed = 0
        self._inflight: dict[str, asyncio.Future] = {}
        self.ffmpeg = _FFMPEG
        self.ext = "mp3" if self.ffmpeg else "wav"
        # 有 ffmpeg 时清掉旧的 wav（否则会优先命中旧 wav，拿不到 mp3 的体积优势）
        if self.ffmpeg:
            try:
                for name in os.listdir(self.cache_dir):
                    if name.endswith(".wav"):
                        os.remove(os.path.join(self.cache_dir, name))
            except OSError:
                pass

    # ---------------------------------------------------------------- 路径
    def path_for(self, post: dict, persona: dict) -> tuple[str, str, str]:
        import hashlib

        content = str(post.get("content") or "")
        content_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
        lang, voice = _voice_for(post, persona)
        safe_voice = voice.replace("/", "_")
        name = f"{post['id']}-{content_hash}-{safe_voice}.{self.ext}"
        return os.path.join(self.cache_dir, name), lang, voice

    async def _to_mp3(self, wav_path: str, mp3_path: str) -> bool:
        """wav → mp3（失败返回 False，调用方回落 wav）。"""
        if not self.ffmpeg:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", wav_path, "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
                "-ac", "1", mp3_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=20)
            return proc.returncode == 0 and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0
        except Exception as exc:  # noqa: BLE001
            log.debug("mp3 转码失败，回落 wav: %s", exc)
            return False

    # ---------------------------------------------------------------- 合成
    async def get_or_synth(self, post: dict, persona: dict) -> tuple[str, str, str]:
        """返回 (音频路径, 语言, 声线)；文件已在则直接命中（毫秒级）。"""
        path, lang, voice = self.path_for(post, persona)
        alt_wav = path[: -len("." + self.ext)] + ".wav"
        for candidate in (path, alt_wav):
            if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                return candidate, lang, voice

        # 同一文件正在合成时，复用同一个任务（防止并发重复合成）
        existing = self._inflight.get(path)
        if existing is not None:
            await asyncio.shield(existing)
            for candidate in (path, alt_wav):
                if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                    return candidate, lang, voice
            return path, lang, voice

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[path] = fut
        try:
            tmp, _duration = await self.voice.synth_to_wav(
                str(post.get("content") or "")[:280], voice, out_dir=self.cache_dir
            )
            target = path
            if self.ext == "mp3":
                if not await self._to_mp3(tmp, path):
                    target = alt_wav
                    shutil.move(tmp, alt_wav)
                else:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            else:
                shutil.move(tmp, path)
            self.done += 1
            fut.set_result(True)
            return target, lang, voice
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(path, None)

    # ---------------------------------------------------------------- 预热
    def enqueue(self, post: dict, persona: dict) -> None:
        try:
            path, _lang, _voice = self.path_for(post, persona)
        except Exception:  # noqa: BLE001
            return
        if path in self._seen:
            return
        self._seen.add(path)
        self._queue.put_nowait((post, persona))

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="v3-audio-prefetch")

    def stop(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    async def _run(self) -> None:
        log.info("v3 朗读音频预热启动（目录 %s）", self.cache_dir)
        while True:
            post, persona = await self._queue.get()
            try:
                path, lang, voice = await self.get_or_synth(post, persona)
                log.debug("预热完成 %s [%s/%s]", os.path.basename(path), lang, voice)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响后续
                log.debug("预热失败 %s: %s", post.get("id"), exc)
            finally:
                self._queue.task_done()

    # ---------------------------------------------------------------- 状态
    def stats(self) -> dict:
        try:
            files = [n for n in os.listdir(self.cache_dir) if n.endswith((".wav", ".mp3"))]
        except OSError:
            files = []
        return {
            "ready": len(files),
            "queued": self._queue.qsize(),
            "generated": self.done,
            "failed": self.failed,
            "format": self.ext,
            "dir": self.cache_dir,
        }

    def warm(self, service: Any, limit: int = WARM_STARTUP_ITEMS) -> int:
        """把前 limit 条 Persona 动态放进预热队列（失败静默）。"""
        count = 0
        try:
            posts = service.recent_persona_posts(limit=limit)  # type: ignore[attr-defined]
        except AttributeError:
            return 0
        for post in posts:
            persona = service.persona_by_id(str(post.get("persona_id") or ""))
            if persona is None:
                continue
            self.enqueue(post, persona)
            count += 1
        return count
