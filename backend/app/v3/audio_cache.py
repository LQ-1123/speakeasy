"""V3 · 信息流朗读音频缓存（预生成，点击即播）。

为什么需要
----------
TTS 合成一条动态要几秒；用户点「朗读」再合成必然卡顿。
信息流是**预生成语料**：内容固定、数量有限，可以在后台提前合成好，
点击时直接读文件（毫秒级）。

音色一致性（第四阶段重点）
--------------------------
朗读必须用**角色自己的声音**，且与聊天/通话完全一致：

* 音色解析统一走 :mod:`app.voice.voice_registry`（不再自己拼 reference_id）；
* 文件名里带 provider token（``fish-<hash8>`` / ``edge-<voice>``）：
  Fish 不可用时缓存的是 Edge 版本，Fish 恢复后 token 变化 → 自动重新合成 Fish 版本，
  **不会**把降级音频当成正式音色一直播下去；
* 不再按帖子语言换声线（Fish 是多语言的），否则同一个人在动态里会变成另一个声音。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from typing import Any

from app.logging_utils import get_logger
from app.voice import voice_registry
from app.v3.seed_data import detect_language

log = get_logger(__name__)

#: 启动时预热多少条（第一页的可见内容）
WARM_STARTUP_ITEMS = 8

#: wav → mp3：体积约降 6-8 倍，传输/解码更快（有 ffmpeg 时启用）
MP3_BITRATE = "64k"
_FFMPEG = os.getenv("SE_FFMPEG") or shutil.which("ffmpeg") or ""


def _binding_for(post: dict, persona: dict) -> voice_registry.VoiceBinding:
    """朗读用角色音色（**不**按帖子语言换声线）。"""
    return voice_registry.binding_for_persona(persona)


def _post_lang(post: dict, persona: dict) -> str:
    content = str(post.get("content") or "")
    persona_lang = str(persona.get("language") or "en").strip().lower()
    return str(post.get("language") or "").strip().lower() or detect_language(content) or persona_lang


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

    # ---------------------------------------------------------------- 路径
    def _base_name(self, post: dict, persona: dict) -> tuple[str, str, str]:
        """返回 (无后缀文件名, 语言, 音色身份 token)。

        文件名里带 ``render_key``（provider + 身份）：降级期间生成的 Edge 版本与
        Fish 版本是不同文件，Fish 恢复后自动重新合成，不会继续播降级音频。
        """
        content = str(post.get("content") or "")
        content_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
        binding = _binding_for(post, persona)
        parameter_key = voice_registry.render_revision(persona)
        safe_key = f"{binding.render_key}-v2-{parameter_key}".replace("/", "_").replace(":", "_")
        return f"{post['id']}-{content_hash}-{safe_key}", _post_lang(post, persona), binding.token

    def _candidates(self, base: str) -> list[str]:
        """已有缓存的可能后缀（优先 mp3，体积小）。"""
        return [os.path.join(self.cache_dir, f"{base}.{ext}") for ext in ("mp3", "wav")]

    def path_for(self, post: dict, persona: dict) -> tuple[str, str, str]:
        """优先返回 mp3 路径（预热/入队用），其余逻辑见 get_or_synth。"""
        base, lang, token = self._base_name(post, persona)
        return self._candidates(base)[0], lang, token

    async def _to_mp3(self, wav_path: str, mp3_path: str) -> bool:
        """wav → mp3（失败返回 False，调用方保留 wav）。"""
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
            return (
                proc.returncode == 0
                and os.path.exists(mp3_path)
                and os.path.getsize(mp3_path) > 0
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("mp3 转码失败，保留 wav: %s", exc)
            return False

    # ---------------------------------------------------------------- 合成
    async def get_or_synth(self, post: dict, persona: dict) -> tuple[str, str, str]:
        """返回 (音频路径, 语言, 音色 token)；文件已在则直接命中（毫秒级）。"""
        base, lang, token = self._base_name(post, persona)
        for candidate in self._candidates(base):
            if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                return candidate, lang, token

        existing = self._inflight.get(base)
        if existing is not None:
            await asyncio.shield(existing)
            for candidate in self._candidates(base):
                if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                    return candidate, lang, token
            raise RuntimeError("朗读音频合成失败")

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[base] = fut
        try:
            text = str(post.get("content") or "")[:280]
            result = await voice_registry.synthesize(
                persona,
                text,
                binding=_binding_for(post, persona),
                edge_engine=self.voice,
                out_dir=self.cache_dir,
            )
            if result is None or not result.data:
                raise RuntimeError("朗读音频合成失败（Fish 与 Edge 均不可用）")

            if result.ext == "wav":
                wav_path = os.path.join(self.cache_dir, f"{base}.wav")
                with open(wav_path, "wb") as fh:  # noqa: PTH123
                    fh.write(result.data)
                mp3_path = os.path.join(self.cache_dir, f"{base}.mp3")
                if self.ffmpeg and await self._to_mp3(wav_path, mp3_path):
                    try:
                        os.remove(wav_path)  # noqa: PTH107
                    except OSError:
                        pass
                    target = mp3_path
                else:
                    target = wav_path
            else:
                target = os.path.join(self.cache_dir, f"{base}.{result.ext}")
                with open(target, "wb") as fh:  # noqa: PTH123
                    fh.write(result.data)

            self.done += 1
            fut.set_result(True)
            return target, lang, token
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(base, None)

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
        fish_files = sum(1 for name in files if "-fish-" in name)
        return {
            "ready": len(files),
            "queued": self._queue.qsize(),
            "generated": self.done,
            "failed": self.failed,
            "format": "mp3" if self.ffmpeg else "wav",
            "fish_files": fish_files,
            "edge_files": len(files) - fish_files,
            "dir": self.cache_dir,
            "fish": voice_registry.fish_status(),
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
