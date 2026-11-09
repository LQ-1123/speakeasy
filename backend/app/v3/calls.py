"""Authenticated persistent call transport; long-lived tokens never enter URLs."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
import hashlib
import hmac
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.v3.call_sessions import CallSessionRegistry
from app.v3.call_runtime import CallRuntime, reply_audio
from app.v3.identity import reset_current_user, set_current_user
from app.v3.security import bind_current_user
from app.voice.deepgram_stream import DeepgramStreamer, deepgram_key

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v3/calls", tags=["calls"])
REGISTRY = CallSessionRegistry(max_active=max(1, int(os.getenv("SE_MAX_ACTIVE_CALLS", "3"))))
RUNTIMES: dict[str, CallRuntime] = {}
MAX_PCM_BYTES = 16000 * 2 * 30


class CallIn(BaseModel):
    friend_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(default="", max_length=100)


async def require_user(request: Request) -> str:
    if not (request.headers.get("x-user-id") or "").strip() or not (request.headers.get("x-user-token") or "").strip():
        raise HTTPException(401, "请先验证身份")
    user = await bind_current_user(request)
    if user == "local":
        raise HTTPException(401, "请先验证身份")
    return user


def payload(call, ticket):
    return {"call_id": call.call_id, "ticket": ticket,
            "ws_path": f"/api/v3/calls/{call.call_id}/stream", "expires_in": 60}


@router.post("")
async def create_call(body: CallIn, request: Request) -> dict:
    user = await require_user(request)
    services = request.app.state.v2
    friend = await asyncio.to_thread(services.db.get_friend, body.friend_id, owner_user_id=user)
    if not friend or friend.get("owner_user_id") != user:
        raise HTTPException(404, "好友不存在")
    try:
        call = REGISTRY.create(user, body.friend_id, friend.get("reply_language") or "en",
                               request_id=body.request_id)
    except OverflowError:
        raise HTTPException(429, "通话繁忙，请稍后重试") from None
    except ValueError:
        raise HTTPException(409, "请求编号已用于其他通话") from None
    RUNTIMES.setdefault(call.call_id, CallRuntime(started_at=time.time(), user_id=user, friend_id=body.friend_id))
    return payload(call, REGISTRY.take_initial_ticket(call))


@router.post("/{call_id}/ticket")
async def renew_ticket(call_id: str, request: Request) -> dict:
    user = await require_user(request)
    try:
        ticket = REGISTRY.issue_ticket(call_id, user)
    except (PermissionError, KeyError):
        raise HTTPException(410, "通话已结束或恢复超时") from None
    # Authenticated takeover beats the old connection's heartbeat timeout.
    runtime = RUNTIMES.get(call_id)
    if runtime and runtime.socket:
        old = runtime.socket
        runtime.socket = None
        runtime.disconnect_version += 1
        runtime.interrupt()
        REGISTRY.disconnect(call_id)
        with contextlib.suppress(Exception):
            await old.close(code=1012, reason="reconnecting")
    return payload(REGISTRY.get_resumable(call_id, user), ticket)


async def finish_call(app, call_id: str, user: str) -> None:
    call = REGISTRY.owned(call_id, user)
    runtime = RUNTIMES.pop(call_id, None)
    if call:
        REGISTRY.end(call_id, user)
    if not runtime:
        return
    runtime.interrupt()
    if runtime.generation and runtime.generation is not asyncio.current_task():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runtime.generation
    if runtime.audio_dir:
        directory = Path(runtime.audio_dir)
        for file in directory.glob('*'):
            if file.is_file():
                with contextlib.suppress(OSError):
                    file.unlink()
        with contextlib.suppress(OSError):
            directory.rmdir()
    if runtime.socket:
        with contextlib.suppress(Exception):
            await runtime.socket.send_json({"type": "ended", "call_id": call_id,
                                           "sequence": runtime.sequence + 1, "turn_id": ""})
            await runtime.socket.close(code=1000)
    if not runtime.summarized:
        runtime.summarized = True
        context = set_current_user(user)
        try:
            seconds = max(0, int(time.time() - runtime.started_at))
            await asyncio.to_thread(app.state.v2.db.add_message, runtime.friend_id, "system",
                f"语音通话 {seconds//60:02d}:{seconds%60:02d} · {runtime.played_turns} 轮",
                msg_type="call_summary", is_read=True,
                context=json.dumps(runtime.history,ensure_ascii=False))
            queue = getattr(app.state, "background_jobs", None)
            if queue and runtime.history:
                queue.enqueue("call_memory", {"user_id":user,"friend_id":runtime.friend_id,
                              "history":runtime.history}, dedupe_key=f"call_memory:{call_id}", priority=150)
        finally:
            reset_current_user(context)


@router.delete("/{call_id}")
async def end_call(call_id: str, request: Request) -> dict:
    user = await require_user(request)
    if REGISTRY.get_resumable(call_id, user) is None:
        raise HTTPException(404, "通话不存在")
    await finish_call(request.app, call_id, user)
    return {"ended": True}


@router.get("/{call_id}/audio/{audio_id}")
async def private_call_audio(call_id: str, audio_id: str, ticket: str = ""):
    runtime = RUNTIMES.get(call_id)
    media = runtime.media.get(audio_id) if runtime else None
    if not media or time.time() >= media['expires']:
        raise HTTPException(404, '音频已过期')
    if not ticket or not hmac.compare_digest(media['token_hash'], hashlib.sha256(ticket.encode()).hexdigest()):
        raise HTTPException(403, '音频凭证无效')
    return Response(media['data'], media_type=media['mime'], headers={'Cache-Control':'private, no-store'})


def retain_private_audio(runtime: CallRuntime, audio_id: str, path: str, audio_dir: str) -> str:
    """Consume the temporary synthesis file; private audio never lives at public V2 URLs."""
    source = Path(path).resolve()
    if source.parent != Path(audio_dir).resolve():
        raise RuntimeError('invalid-audio-path')
    try:
        if source.stat().st_size > 4_000_000:
            raise RuntimeError('audio-too-large')
        data = source.read_bytes()
    finally:
        source.unlink(missing_ok=True)
    if not data:
        raise RuntimeError('speech-unavailable')
    now = time.time()
    for key, value in list(runtime.media.items()):
        if value['expires'] <= now:
            runtime.media.pop(key, None)
    if sum(len(v['data']) for v in runtime.media.values()) + len(data) > 12_000_000:
        raise RuntimeError('audio-buffer-full')
    token = secrets.token_urlsafe(24)
    runtime.media[audio_id] = {'data':data,'expires':now+90,
                              'token_hash':hashlib.sha256(token.encode()).hexdigest(),
                              'mime':'audio/mpeg' if source.suffix=='.mp3' else 'audio/wav'}
    return token


@router.websocket("/{call_id}/stream")
async def call_stream(ws: WebSocket, call_id: str) -> None:
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), 5)
        call = REGISTRY.resolve_ticket(call_id, str(auth.get("ticket") or "")) if isinstance(auth, dict) and auth.get("type") == "auth" else None
    except (ValueError, asyncio.TimeoutError, WebSocketDisconnect):
        call = None
    if call is None:
        await ws.close(code=4401, reason="invalid-call-ticket")
        return
    context = set_current_user(call.user_id)
    runtime = RUNTIMES.setdefault(call_id, CallRuntime(started_at=time.time(), user_id=call.user_id, friend_id=call.friend_id))
    runtime.socket = ws
    runtime.disconnect_version += 1
    socket_version = runtime.disconnect_version
    services = ws.app.state.v2
    friend = await asyncio.to_thread(services.db.get_friend, call.friend_id, owner_user_id=call.user_id)
    if not friend or friend.get("owner_user_id") != call.user_id:
        await ws.close(code=4404)
        REGISTRY.end(call_id, call.user_id)
        RUNTIMES.pop(call_id, None)
        reset_current_user(context)
        return
    send_lock = asyncio.Lock()
    pcm = bytearray()
    streamer = None
    stt_task = None
    heartbeat = None
    finalize_task = None
    final_segments: list[str] = []
    finalized = asyncio.Event()
    muted = False
    ended = False
    last_received = time.monotonic()
    stt_sent_seconds = 0.0
    stt_accept_after = 0.0

    async def emit(kind: str, turn_id: str = "", **fields):
        async with send_lock:
            runtime.sequence += 1
            await ws.send_json({"type": kind, "call_id": call_id, "turn_id": turn_id,
                                "sequence": runtime.sequence, **fields})

    async def report(code: str, turn_id: str = ""):
        messages = {"empty-transcript": "没有听清，请再说一次", "speech-unavailable": "声音暂不可用，请重试",
                    "model-unavailable": "对话服务暂不可用", "wrong-language": "回复语言异常，请重试",
                    "playback-failed": "声音播放失败，请重试", "audio-too-long": "请分成短句再说一次"}
        await emit("error", turn_id, code=code, message=messages.get(code, "连接暂时不可用，请重试"), recoverable=True)

    async def generate(text: str, turn_id: str):
        first_at = time.monotonic()
        try:
            runtime.audio_dir = str(Path(services.chat.audio.audio_dir).parent / 'private_call_audio' / call_id)
            await emit("transcript", turn_id, text=text, final=True)
            await emit("responding", turn_id)
            async with asyncio.timeout(45):
                async for audio in reply_audio(services.chat, friend, text, list(runtime.history), audio_dir=runtime.audio_dir):
                    if runtime.current_turn != turn_id:
                        return
                    audio_id = uuid.uuid4().hex
                    media_ticket = await asyncio.to_thread(retain_private_audio, runtime, audio_id,
                        audio['path'], runtime.audio_dir)
                    runtime.offer_audio(audio_id, turn_id, audio["text"], audio["index"])
                    await emit("audio", turn_id, audio_id=audio_id, text=audio["text"],
                               audio_url=f"/api/v3/calls/{call_id}/audio/{audio_id}?ticket={media_ticket}",
                               duration_ms=audio.get("duration_ms", 0), index=audio["index"])
                    if audio["index"] == 0:
                        log.info("call metric call=%s turn=%s first_audio_ms=%.0f", call_id, turn_id, (time.monotonic()-first_at)*1000)
                await emit("turn_done", turn_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("call failure call=%s turn=%s kind=%s", call_id, turn_id, type(exc).__name__)
            if runtime.current_turn == turn_id:
                await report(str(exc) if isinstance(exc, RuntimeError) else "call-turn-failed", turn_id)

    async def start_text(text: str, turn_id: str):
        if not text.strip():
            await report("empty-transcript", turn_id)
            return
        if runtime.begin_turn(turn_id, text.strip()):
            runtime.generation = asyncio.create_task(generate(text.strip(), turn_id), name=f"call-reply:{call_id}")

    async def finalize_turn(turn_id: str, audio: bytes):
        nonlocal stt_accept_after
        try:
            if streamer and stt_task and not stt_task.done():
                finalized.clear()
                await streamer.finalize()
                try:
                    await asyncio.wait_for(finalized.wait(), 2.5)
                    text = " ".join(final_segments).strip()
                except asyncio.TimeoutError:
                    text = ""
                final_segments.clear()
                stt_accept_after = stt_sent_seconds
                if not text:
                    text, meta = await asyncio.wait_for(services.chat._transcribe(audio, language=call.language, fast=True), 12)
                    if meta.get("backend") in ("mock", "tone", "error"):
                        text = ""
            else:
                text, meta = await asyncio.wait_for(services.chat._transcribe(
                    audio, language=call.language, fast=True), 12)
                if meta.get("backend") in ("mock", "tone", "error"):
                    text = ""
            await start_text(text, turn_id)
        except asyncio.TimeoutError:
            final_segments.clear()
            await report("stt-timeout", turn_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            final_segments.clear()
            await report("stt-unavailable", turn_id)

    async def pump_stt():
        try:
            async for event in streamer.events():
                if event.get("start", 0) + .001 < stt_accept_after:
                    continue  # Late recognition from a completed/interrupted utterance.
                if event["type"] == "final" and event.get("text"):
                    final_segments.append(event["text"])
                if event.get("finalized"):
                    finalized.set()
                if event["type"] == "partial":
                    await emit("transcript", text=event.get("text", ""), final=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("call recognizer disconnected call=%s", call_id)
        finally:
            finalized.set()

    async def keepalive():
        while True:
            await asyncio.sleep(10)
            if time.monotonic()-last_received > 35:
                await ws.close(code=1011, reason="heartbeat-timeout")
                return
            if streamer and stt_task and not stt_task.done():
                with contextlib.suppress(Exception):
                    await streamer.keepalive()

    try:
        key = deepgram_key()
        if key:
            candidate = DeepgramStreamer(key, call.language)
            try:
                await asyncio.wait_for(candidate.connect(), 5)
                streamer = candidate
                stt_task = asyncio.create_task(pump_stt(), name=f"call-stt:{call_id}")
            except Exception:
                await candidate.close()
        await emit("ready", language=call.language, streaming_stt=streamer is not None)
        heartbeat = asyncio.create_task(keepalive(), name=f"call-heartbeat:{call_id}")
        while True:
            message = await ws.receive()
            last_received = time.monotonic()
            if message["type"] == "websocket.disconnect":
                break
            audio = message.get("bytes")
            if audio:
                if muted:
                    continue
                if len(audio) % 2 or len(audio) > 32000 or len(pcm) + len(audio) > MAX_PCM_BYTES:
                    pcm.clear()
                    final_segments.clear()
                    await report("audio-too-long")
                    continue
                if finalize_task and not finalize_task.done():
                    continue
                pcm.extend(audio)
                if streamer and stt_task and not stt_task.done():
                    try:
                        await asyncio.wait_for(streamer.send(audio), 2)
                        stt_sent_seconds += len(audio) / 32000
                    except Exception:
                        stt_task.cancel()
                continue
            raw = message.get("text") or ""
            if len(raw) > 10000:
                await ws.close(code=1009)
                break
            try:
                command = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(command, dict):
                continue
            kind = command.get("type")
            turn_id = str(command.get("turn_id") or "")[:100]
            if kind == "ping":
                await emit("pong")
            elif kind == "mute":
                muted = bool(command.get("muted"))
                pcm.clear()
                final_segments.clear()
                stt_accept_after = stt_sent_seconds
            elif kind == "interrupt":
                runtime.interrupt()
                if finalize_task and not finalize_task.done():
                    finalize_task.cancel()
                pcm.clear()
                final_segments.clear()
                stt_accept_after = stt_sent_seconds
                await emit("interrupted", turn_id)
            elif kind == "utterance_end":
                if not muted and turn_id and pcm and not (finalize_task and not finalize_task.done()):
                    if turn_id in runtime.seen_turns:
                        pcm.clear()
                        continue
                    audio = bytes(pcm)
                    pcm.clear()
                    finalize_task = asyncio.create_task(finalize_turn(turn_id, audio), name=f"call-finalize:{call_id}")
            elif kind == "text":
                await start_text(str(command.get("text") or "")[:2000], turn_id)
            elif kind == "playback_done":
                if runtime.acknowledge(str(command.get("audio_id") or ""), turn_id):
                    runtime.media.pop(str(command.get("audio_id") or ""), None)
                    log.info("call played call=%s turn=%s", call_id, turn_id)
            elif kind == "playback_failed":
                if turn_id == runtime.current_turn:
                    runtime.interrupt()
                    await emit("interrupted", turn_id)
                    await report("playback-failed", turn_id)
            elif kind == "end":
                ended = True
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        owns_socket = runtime.socket is ws
        if owns_socket:
            runtime.interrupt()
        for task in (runtime.generation if owns_socket else None, stt_task, heartbeat, finalize_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if streamer:
            await streamer.close()
        if runtime.socket is ws:
            runtime.socket = None
        elif runtime.disconnect_version != socket_version:
            reset_current_user(context)
            return
        if ended:
            await finish_call(ws.app, call_id, call.user_id)
        elif REGISTRY.contains(call_id):
            REGISTRY.disconnect(call_id)
            async def expire():
                await asyncio.sleep(20)
                if runtime.socket is None and RUNTIMES.get(call_id) is runtime and runtime.disconnect_version == socket_version:
                    await finish_call(ws.app, call_id, call.user_id)
            asyncio.create_task(expire(), name=f"call-expiry:{call_id}")
        reset_current_user(context)


async def cleanup_sessions(app):
    """Reap abandoned POSTs and disconnected runtimes without losing summaries."""
    while True:
        await asyncio.sleep(5)
        REGISTRY.active_count()
        for call_id, runtime in list(RUNTIMES.items()):
            if not REGISTRY.contains(call_id):
                await finish_call(app, call_id, runtime.user_id)
