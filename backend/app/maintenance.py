"""One durable maintenance worker; active calls preempt low-priority generation."""
from __future__ import annotations

import asyncio
import os
import time

from app.jobs import DurableJobQueue
from app.logging_utils import get_logger

log = get_logger(__name__)


async def maintenance_loop(app, settings, engines, queue: DurableJobQueue) -> None:
    from app.v3 import (
        calls,
        daily_world,
        event_posts,
        lexicon,
        life_events,
        lifecycle,
        speech_dna,
        world,
    )
    from app.voice.catalog import ensure_ready_personas, ensure_ready_friends, repair_persona_voices
    from app.admin import media_admin
    from app.v2.audio_retention import enforce_cap, MAX_BYTES
    from app.v3.followup_jobs import schedule_followups, deliver_followup

    db = app.state.v2.db
    media_dir = os.path.join(str(settings.data_path), "media")

    async def world_job(payload):
        lexicon.refresh()
        with db._lock:
            before = {str(r[0]) for r in db.conn.execute("SELECT id FROM personas")}
        await world.ensure_world(db, engines.llm)
        # World bootstrap predates readiness. Existing identities stay active; new
        # identities must complete synthesis verification before public exposure.
        with db._lock, db.conn:
            rows = db.conn.execute("SELECT id FROM personas").fetchall()
            for row in rows:
                if str(row[0]) not in before:
                    db.conn.execute("UPDATE personas SET status='waiting_voice' WHERE id=?", (row[0],))
        repair_persona_voices(db)
        app.state.v3._persona_cache["at"] = 0

    async def daily_job(payload):
        await daily_world.generate_daily(db, engines.llm, day=payload["day"], media_dir=media_dir)
        app.state.v3._persona_cache["at"] = 0

    async def life_job(payload):
        await asyncio.to_thread(life_events.refresh_statuses, db)
        await life_events.generate_day_events(db, engines.llm, day=payload["day"])

    async def dna_job(payload):
        with db._lock:
            rows = [dict(r) for r in db.conn.execute("SELECT * FROM personas WHERE status='active'")]
        await speech_dna.ensure_persona_dna(db, engines.llm, rows)
        await asyncio.to_thread(life_events.ensure_relationships, db)

    async def voices_job(payload):
        if await ensure_ready_personas(db, limit=3):
            app.state.v3._persona_cache["at"] = 0

    async def followups_job(payload):
        await asyncio.to_thread(schedule_followups, app.state.v2, queue)
        await ensure_ready_friends(db, limit=3)

    async def call_memory_job(payload):
        from app.v3.identity import set_current_user, reset_current_user
        from app.v3.account_archive import user_is_available
        if not user_is_available(db, payload['user_id']):
            return
        token = set_current_user(payload['user_id'])
        try:
            friend = db.get_friend(payload['friend_id'], owner_user_id=payload['user_id'])
            if friend:
                recent = [{'sender':'user' if item['role']=='user' else 'friend',
                           'transcript':item['content']} for item in payload['history']]
                await app.state.v2.memory.after_turn(friend, recent_messages=recent)
        finally:
            reset_current_user(token)

    async def audio_job(payload):
        cache = app.state.v3_audio
        for post in app.state.v3.recent_persona_posts(limit=3):
            if calls.REGISTRY.active_count():
                raise RuntimeError("maintenance_paused_for_call")
            persona = app.state.v3.persona_by_id(post["persona_id"])
            if persona:
                await cache.get_or_synth(post, persona)

    async def cleanup_job(payload):
        await asyncio.to_thread(lifecycle.cleanup, db)
        await asyncio.to_thread(media_admin.prune, db)
        # 商业合作留痕：30 天后删除触发摘录（品牌/活动/投放记录保留 ≥3 年）
        from app.v3 import ads as _ads  # noqa: PLC0415

        await asyncio.to_thread(_ads.cleanup_audit, db)
        await asyncio.to_thread(enforce_cap,
            db_path=db.path, audio_dir=os.path.join(str(settings.data_path), "audio"), max_bytes=MAX_BYTES)

    async def posts_job(payload):
        # 节奏参数从词库读（保存即生效）。环境变量若设置仍优先，
        # 见 app/v3/lexicon.py::rhythm_params 的优先级说明。
        from app.v3 import lexicon as _lex  # noqa: PLC0415

        rhythm = _lex.rhythm_params()
        await event_posts.run_posts_cycle(
            db,
            engines.llm,
            max_events=max(1, int(rhythm.get("posts_max_events", 4))),
            since_hours=max(1.0, float(rhythm.get("posts_since_hours", 6.0))),
        )

    handlers = {"world": world_job, "daily_world": daily_job, "life_events": life_job,
                "persona_dna": dna_job, "voice_readiness": voices_job,
                "audio_warm": audio_job, "cleanup": cleanup_job, "event_posts": posts_job,
                "call_memory": call_memory_job, "followups": followups_job,
                "followup": lambda p: deliver_followup(app.state.v2,p)}
    boot = str(time.time_ns())
    queue.enqueue("world", {}, dedupe_key=f"world:startup:{boot}", priority=100)
    running = None
    try:
        while True:
            now = time.time()
            day = daily_world.day_key()
            # 每轮重新读节奏参数：运营在后台保存后**下一轮即生效**，无需重启。
            # （2026-11 前的实现直接读 os.getenv，导致这 4 个参数改了等于没改。）
            rhythm = lexicon.rhythm_params()
            periods = {
                "life_events": max(5.0, rhythm["life_tick_s"]),
                "event_posts": max(60.0, rhythm["posts_tick_s"]),
                "voice_readiness": 300, "audio_warm": 600, "cleanup": 3600,
                "persona_dna": 3600, "followups": 60,
            }
            queue.enqueue("daily_world", {"day": day}, dedupe_key=f"daily_world:{day}", priority=120)
            for kind, period in periods.items():
                queue.enqueue(kind, {"day": day}, dedupe_key=f"{kind}:{int(now // period)}",
                              priority=110 if kind == "voice_readiness" else 200)
            if calls.REGISTRY.active_count():
                await asyncio.sleep(1)
                continue
            running = asyncio.create_task(queue.run_one_async(handlers), name="maintenance-job")
            while not running.done():
                done, _ = await asyncio.wait({running}, timeout=0.25)
                if done:
                    break
                if calls.REGISTRY.active_count():
                    running.cancel()
                    break
            try:
                worked = await running
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                worked = False
            running = None
            await asyncio.sleep(0.1 if worked else 1)
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
