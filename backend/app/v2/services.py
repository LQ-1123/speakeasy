"""v2 服务装配：把 SQLite、记忆、捏人、语音、聊天服务串起来，挂在 ``app.state.v2``。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from app.logging_utils import get_logger
from app.v2.audio_store import AudioStore
from app.v2.chat_service import ChatService
from app.v2.db import DB
from app.v2.media_store import MediaStore
from app.v2.memory import MemoryManager
from app.v2.spawner import PersonaSpawner
from app.v2.voices import FriendVoiceSynth

log = get_logger(__name__)


@dataclass
class V2Services:
    db: DB
    engines: Any
    audio: AudioStore
    memory: Any
    voice: Any
    spawner: Any
    chat: ChatService
    data_dir: str
    onboarding_task: asyncio.Task | None = None
    followup_inflight: set[str] = field(default_factory=set)
    _tasks: set[asyncio.Task] = field(default_factory=set)
    media: Any = None

    def spawn_bg(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def onboarding_pending(self) -> bool:
        return self.onboarding_task is not None and not self.onboarding_task.done()

    async def stop_workers(self) -> None:
        if getattr(self, '_workers_stopped', False):
            return
        await self.chat.aclose()
        if self.onboarding_task is not None and not self.onboarding_task.done():
            self.onboarding_task.cancel()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers_stopped = True

    async def aclose(self) -> None:
        await self.stop_workers()
        pool = getattr(self, 'provider_pool', None)
        if pool is not None:
            await pool.aclose()
            from app.providers.runtime import unbind_pool
            unbind_pool(pool)
        try:
            await asyncio.to_thread(self.db.close)
        except Exception as exc:  # noqa: BLE001
            log.debug("关闭 v2 db 失败: %s", exc)


def build_services(engines: Any, data_dir: str) -> V2Services:
    os.makedirs(data_dir, exist_ok=True)
    db = DB(os.path.join(data_dir, "speakeasy_v2.db"))
    db.init()
    audio = AudioStore(data_dir)
    media = MediaStore(data_dir)
    memory = MemoryManager(db, engines.llm)
    voice = FriendVoiceSynth()
    spawner = PersonaSpawner(db, engines.llm)
    idle_s = float(os.getenv("V2_MEMORY_IDLE_S", "120") or 120)
    chat = ChatService(
        engines, db, memory, voice, spawner, audio, media, memory_idle_s=idle_s
    )
    # In-process synthesis cannot survive a restart. Do not leave old pending
    # bubbles waiting forever; explicit retry reuses the original message.
    with db._lock, db.conn:
        db.conn.execute("UPDATE messages SET audio_status='failed', audio_error=? "
                        "WHERE audio_status IN ('queued','generating') AND COALESCE(audio_path,'')=''",
                        ('服务已重启，请重试语音',))
    services = V2Services(
        db=db,
        engines=engines,
        audio=audio,
        media=media,
        memory=memory,
        voice=voice,
        spawner=spawner,
        chat=chat,
        data_dir=data_dir,
    )
    log.info("v2 services ready: db=%s audio=%s", db.path, audio.audio_dir)
    return services
