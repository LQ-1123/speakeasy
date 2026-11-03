"""WebSocket 路由：唯一的前后端实时通道。

协议权威定义见 ``docs/PROTOCOL.md``。本模块只负责**连接生命周期 + 消息分发**，
真正的一轮对话在 :class:`~app.pipeline.orchestrator.TurnOrchestrator` 里跑。

设计要点
--------
1. **接收循环绝不被阻塞**：VAD 触发出的一句话会 ``create_task`` 交给后台，
   接收循环立刻回到 ``receive()``。这样才能：
   - 用户说得快时连续识别；
   - 说话期间仍能收到 ``interrupt``（barge-in）；
   - 仍能响应 ``ping``。
2. **一轮未结束又来一轮** → 先 ``cancel()`` 上一轮（真实对话里人就是这样打断的）。
3. **任何异常都不许断连**：全部包进 try/except，转成 ``error`` 消息推回去。
4. **二进制帧 = 原始 PCM16@16k**；文本帧 = JSON。两者混在同一个 receive 循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.logging_utils import get_logger
from app.pipeline.orchestrator import TurnOrchestrator
from app.session import Session, SessionStore
from app.voice.vad import StreamingVAD, VADEvent, VADResult

log = get_logger(__name__)

router = APIRouter()

PROTOCOL_VERSION = 1
INPUT_SAMPLE_RATE = 16000

#: 握手等待上限（秒）
HELLO_TIMEOUT_S = 15.0
#: 单条 JSON 文本帧上限（字符）
MAX_TEXT_FRAME = 200_000


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _engines(ws: WebSocket) -> Any:
    return ws.app.state.engines


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> bool:
    """尽力发送；连接已死返回 False，不抛异常。"""
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
        return True
    except (WebSocketDisconnect, RuntimeError, ConnectionError, asyncio.CancelledError):
        return False
    except Exception as exc:  # noqa: BLE001 - 发送失败不应冒泡
        log.debug("send failed: %s", exc)
        return False


def _err(code: str, message: str, *, fatal: bool = False, turn_id: int | None = None,
         **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "error",
        "code": code,
        "message": message,
        "fatal": fatal,
    }
    if turn_id is not None:
        payload["turn_id"] = turn_id
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# 单连接会话状态
# --------------------------------------------------------------------------- #
class _Connection:
    """一个 WS 连接的全部可变状态。"""

    def __init__(self, ws: WebSocket) -> None:
        engines = _engines(ws)
        self.ws = ws
        self.engines = engines
        self.settings = get_settings()

        self.session: Session | None = None
        self.vad: StreamingVAD = StreamingVAD(self.settings.vad)
        #: 按住说话模式：True 时 VAD 的 SPEECH_END 只累积不提交
        self.ptt_hold: bool = False
        #: hold 期间累积的音频片段（一次按住可能有多个语音段）
        self._ptt_parts: list[bytes] = []
        #: 每个角色一个独立 Session —— 切回角色时上下文还在，且互不污染
        self._persona_sessions: dict[str, Session] = {}
        self.orchestrator: TurnOrchestrator = TurnOrchestrator(
            llm=engines.llm,
            tts=engines.tts,
            personas=engines.personas,
            suggestions=engines.suggestions,
            cfg=self.settings.pipeline,
        )

        self._turn_task: asyncio.Task[None] | None = None
        self._turn_lock = asyncio.Lock()
        self._closed = False
        self.welcomed = False

    # ---- 生命周期 ----
    async def cleanup(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            self.orchestrator.cancel()
        task = self._turn_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.orchestrator.aclose()

    # ---- 持久化 ----
    @property
    def conv_store(self):
        return getattr(self.ws.app.state, "conversation_store", None)

    def history_payload(self) -> list[dict]:
        store = self.conv_store
        if store is None or self.session is None:
            return []
        return store.load(self.session.persona_id)

    async def send_history(self) -> None:
        msgs = self.history_payload()
        await _send_json(
            self.ws,
            {"type": "history", "persona_id": self.session.persona_id, "messages": msgs},
        )

    def record(self, role: str, text: str, *, image: str | None = None) -> None:
        store = self.conv_store
        if store is None or self.session is None:
            return
        try:
            store.append(self.session.persona_id, {"role": role, "text": text, "image": image})
        except Exception as exc:  # noqa: BLE001 - 落盘失败不能影响对话
            log.warning("对话落盘失败: %s", exc)

    # ---- 音频输入 ----
    #: hold 模式最多累积多少秒（超出丢最早的），防内存爆
    PTT_MAX_SECONDS = 60

    async def feed_audio(self, frame: bytes) -> None:
        """喂一帧 PCM16@16k 给 VAD；触发 SPEECH_END 就起一轮。

        按住模式（ptt_hold）不同：直接累积原始 PCM，完全绕过 VAD 判定。
        微信语义是"从按到松 = 一条消息"，中间停顿不该切段，也不该提前提交。
        """
        if self._closed or not frame:
            return
        if self.ptt_hold:
            self._ptt_parts.append(frame)
            # 超过上限就丢最早的帧
            max_bytes = self.PTT_MAX_SECONDS * INPUT_SAMPLE_RATE * 2
            total = sum(len(p) for p in self._ptt_parts)
            while total > max_bytes and len(self._ptt_parts) > 1:
                total -= len(self._ptt_parts.pop(0))
            return
        try:
            result = self.vad.push(frame)
        except Exception as exc:  # noqa: BLE001
            log.warning("VAD push failed: %s", exc)
            await _send_json(self.ws, _err("vad_failed", f"VAD 异常: {exc}"))
            return
        await self._handle_vad(result)

    async def _handle_vad(self, result: VADResult) -> None:
        if not result or result.event is None:
            return
        if result.event is VADEvent.SPEECH_START:
            await _send_json(self.ws, {"type": "vad", "state": "speech_start"})
            return
        if result.event is VADEvent.SPEECH_END:
            await _send_json(
                self.ws,
                {"type": "vad", "state": "speech_end", "speech_ms": int(result.speech_ms or 0)},
            )
            if result.utterance:
                if self.ptt_hold:
                    # 按住模式：先存着，等松手（audio_end）再一起提交
                    self._ptt_parts.append(result.utterance)
                else:
                    await self._start_utterance(result.utterance)
            return

    async def _start_utterance(self, pcm: bytes) -> None:
        """后台任务：STT → user_text → orchestrator。"""
        if self._closed:
            return
        task = asyncio.create_task(self._run_utterance(pcm), name="utterance")
        self._turn_task = task
        task.add_done_callback(lambda _t: None)

    async def _run_utterance(self, pcm: bytes) -> None:
        engines = self.engines
        session = self.session
        if session is None:
            return

        # ---- 1. STT ----
        t0 = time.perf_counter()
        try:
            transcript = await engines.stt.transcribe(pcm, sample_rate=INPUT_SAMPLE_RATE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("STT failed: %s", exc)
            await _send_json(self.ws, _err("stt_failed", f"语音识别失败: {exc}"))
            return
        stt_ms = (time.perf_counter() - t0) * 1000.0

        text = (getattr(transcript, "text", "") or "").strip()
        if text:
            img = getattr(self, "_pending_image", None)
            self._pending_image = None
            self.record("user", text, image=img)
        if not text:
            await _send_json(self.ws, {"type": "vad", "state": "idle"})
            return

        # ---- 2. 通知前端（协议 2.3）----
        turn_id = session.next_turn_id()
        await _send_json(
            self.ws,
            {
                "type": "user_text",
                "turn_id": turn_id,
                "text": text,
                "latency": {"stt_ms": round(stt_ms, 1)},
                "truncated": False,
            },
        )

        # ---- 3. 跑一轮 ----
        await self.run_turn(text, turn_id=turn_id, stt_ms=stt_ms)

    async def run_turn(self, user_text: str, *, turn_id: int | None = None,
                       stt_ms: float = 0.0,
                       images: "list[str] | tuple[str, ...] | None" = None) -> None:
        """串行化地跑一轮（同一时刻只有一轮在跑）。"""
        if self._closed:
            return
        session = self.session
        if session is None:
            return

        async with self._turn_lock:
            # 上一轮还在跑（比如用户连发两句）→ 先打断
            if self.orchestrator.is_running:
                log.debug("barge-in: 新回合取消上一回合")
                with contextlib.suppress(Exception):
                    self.orchestrator.cancel()
                await asyncio.sleep(0)

            async def send(payload: dict[str, Any]) -> None:
                await _send_json(self.ws, payload)

            try:
                _acc: list[str] = []

                async def _send_spy(payload, _inner=None):
                    try:
                        t = payload.get("type") if isinstance(payload, dict) else None
                        if t == "assistant_delta":
                            _acc.append(payload.get("text") or "")
                        elif t == "assistant_done":
                            text = (payload.get("text") or "").strip() or "".join(_acc).strip()
                            if text:
                                self.record("assistant", text)
                    except Exception:  # noqa: BLE001 - 观测失败绝不影响发送
                        pass
                    return await send(payload)

                await self.orchestrator.run_turn(
                    session=session,
                    user_text=user_text,
                    send=_send_spy,
                    turn_id=turn_id,
                    stt_ms=stt_ms,
                    images=list(images or ()),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 一轮失败不代表连接该断
                log.exception("turn failed")
                await _send_json(self.ws, _err("internal", f"回合处理失败: {exc}"))


# --------------------------------------------------------------------------- #
# 握手
# --------------------------------------------------------------------------- #
async def _do_hello(conn: _Connection, data: dict[str, Any]) -> bool:
    """处理 hello；成功返回 True。"""
    engines = conn.engines
    personas = engines.personas

    persona_id = data.get("persona_id") or personas.default_id
    if not personas.has(str(persona_id)):
        await _send_json(
            conn.ws,
            _err("persona_not_found", f"未知角色: {persona_id}", fatal=True),
        )
        return False

    rate = data.get("input_sample_rate", INPUT_SAMPLE_RATE)
    try:
        rate = int(rate)
    except (TypeError, ValueError):
        rate = -1
    if rate != INPUT_SAMPLE_RATE:
        await _send_json(
            conn.ws,
            _err(
                "unsupported_sample_rate",
                f"上行采样率必须是 {INPUT_SAMPLE_RATE} Hz，收到 {rate}。"
                "请在浏览器端重采样后再发送。",
                fatal=True,
            ),
        )
        return False

    mode_id = data.get("mode")
    if mode_id is not None and personas.mode(str(mode_id)) is None:
        mode_id = None

    store: SessionStore = conn.ws.app.state.session_store
    conn.session = store.create(str(persona_id), mode_id)
    conn._persona_sessions[str(persona_id)] = conn.session
    conn.vad.reset()

    # 回放该角色的历史对话（刷新页面后也能接着聊）
    with contextlib.suppress(Exception):
        await conn.send_history()

    snapshot = {}
    with contextlib.suppress(Exception):
        snapshot = engines.snapshot()

    audio_out = int(getattr(conn.settings.tts, "sample_rate", 24000) or 24000)
    await _send_json(
        conn.ws,
        {
            "type": "ready",
            "session_id": conn.session.session_id,
            "protocol_version": PROTOCOL_VERSION,
            "persona_id": conn.session.persona_id,
            "mode": conn.session.mode_id or "free",
            "personas": personas.public_list(),
            "modes": personas.mode_public_list(),
            "engines": {
                "stt": snapshot.get("stt", "unknown"),
                "tts": snapshot.get("tts", "unknown"),
                "llm": snapshot.get("llm", {}),
            },
            "audio": {
                "format": "pcm_s16le",
                "input_sample_rate": INPUT_SAMPLE_RATE,
                "sample_rate": audio_out,
                "channels": 1,
            },
        },
    )
    conn.welcomed = True
    log.info(
        "ws ready session=%s persona=%s mode=%s",
        conn.session.session_id[:8], conn.session.persona_id, conn.session.mode_id,
    )
    return True


# --------------------------------------------------------------------------- #
# 文本帧分发
# --------------------------------------------------------------------------- #
async def _handle_text_frame(conn: _Connection, raw: str) -> bool:
    """处理一条 JSON 文本帧；返回 False 表示应关闭连接。"""
    ws = conn.ws
    engines = conn.engines

    if len(raw) > MAX_TEXT_FRAME:
        await _send_json(ws, _err("bad_message", "文本帧过大"))
        return True

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        await _send_json(ws, _err("bad_message", f"JSON 解析失败: {exc}"))
        return True
    if not isinstance(data, dict):
        await _send_json(ws, _err("bad_message", "消息必须是 JSON 对象"))
        return True

    mtype = data.get("type")

    # ---- 握手 ----
    if not conn.welcomed:
        if mtype != "hello":
            await _send_json(
                ws, _err("bad_message", "第一条消息必须是 hello", fatal=True)
            )
            return False
        return await _do_hello(conn, data)

    session = conn.session
    assert session is not None  # noqa: S101 - 已握手

    # ---- 控制 ----
    if mtype == "ping":
        await _send_json(
            ws,
            {
                "type": "pong",
                "t_ms": data.get("t_ms"),
                "server_time": int(time.time() * 1000),
            },
        )
        return True

    if mtype == "hello":
        await _send_json(ws, _err("bad_message", "重复的 hello"))
        return True

    # ---- 清空当前角色的对话记录 ----
    if mtype == "clear_history":
        pid = conn.session.persona_id
        with contextlib.suppress(Exception):
            conn.session.history.clear()
            conn.session.last_user_text = None
            conn.session.turn_counter = 0
            conn._ptt_parts.clear()  # noqa: SLF001
        store = getattr(ws.app.state, "conversation_store", None)
        removed = False
        if store is not None:
            with contextlib.suppress(Exception):
                removed = store.clear(pid)
        log.info("清空对话: persona=%s removed=%s", pid, removed)
        await _send_json(
            ws,
            {"type": "history", "persona_id": pid, "messages": [], "cleared": True},
        )
        return True

    # ---- 按住说话：按下 ----
    if mtype == "ptt_hold":
        conn.ptt_hold = True
        conn._ptt_parts.clear()  # noqa: SLF001 - 同模块协作
        with contextlib.suppress(Exception):
            conn.vad.reset()
        await _send_json(ws, {"type": "ptt", "state": "holding"})
        return True

    # ---- 按住说话：上滑取消（丢弃，不产生任何一轮）----
    if mtype == "ptt_cancel":
        conn.ptt_hold = False
        conn._ptt_parts.clear()  # noqa: SLF001
        with contextlib.suppress(Exception):
            conn.vad.reset()
        # 万一已经起了一轮（极端时序），也取消掉
        with contextlib.suppress(Exception):
            conn.orchestrator.cancel()
        await _send_json(ws, {"type": "ptt", "state": "cancelled"})
        log.debug("ptt cancelled: 已丢弃缓冲音频")
        return True

    if mtype == "audio_end":
        # 按住模式：把 hold 期间累积的片段拼起来一次性提交
        if conn.ptt_hold:
            conn.ptt_hold = False
            with contextlib.suppress(Exception):
                tail = conn.vad.flush()
                if tail and getattr(tail, "utterance", None):
                    conn._ptt_parts.append(tail.utterance)  # noqa: SLF001
            pcm = b"".join(conn._ptt_parts)  # noqa: SLF001
            conn._ptt_parts.clear()  # noqa: SLF001
            # 太短（<0.2s）当成误触，不提交
            min_bytes = int(0.2 * INPUT_SAMPLE_RATE * 2)
            if len(pcm) >= min_bytes:
                log.debug("ptt commit: %.2fs", len(pcm) / 2 / INPUT_SAMPLE_RATE)
                await conn._start_utterance(pcm)
            else:
                await _send_json(ws, {"type": "vad", "state": "idle"})
            return True

        if conn.vad.is_speaking:
            result = conn.vad.flush()
            await conn._handle_vad(result)  # noqa: SLF001 - 同模块协作
        else:
            # 没在说话也可能是 flush 出了残留
            with contextlib.suppress(Exception):
                result = conn.vad.flush()
                if result and result.utterance:
                    await conn._handle_vad(result)
        return True

    if mtype == "interrupt":
        log.debug("interrupt received")
        with contextlib.suppress(Exception):
            conn.orchestrator.cancel()
        await _send_json(ws, {"type": "interrupted"})
        return True

    if mtype == "set_persona":
        pid = data.get("persona_id")
        personas = engines.personas
        if not pid or not personas.has(str(pid)):
            await _send_json(ws, _err("persona_not_found", f"未知角色: {pid}"))
            return True
        pid = str(pid)
        # 每个角色一个独立会话：切回旧角色能续上它自己的上下文，
        # 也不会把 A 的对话带进 B 的 prompt。
        bucket = conn._persona_sessions  # noqa: SLF001
        if pid not in bucket:
            store = getattr(ws.app.state, "session_store", None)
            if store is not None:
                bucket[pid] = store.create(pid, session.mode_id)
            else:  # pragma: no cover - 兜底
                bucket[pid] = Session(session_id=f"{session.session_id}-{pid}", persona_id=pid)

        conn.session = bucket[pid]  # noqa: SLF001
        conn.session.mode_id = session.mode_id or conn.session.mode_id
        conn.session.persona_id = pid

        await _send_json(
            ws,
            {
                "type": "persona_changed",
                "persona_id": pid,
                "turn_id": conn.session.turn_counter,
                "history_len": len(conn.session.history),
            },
        )
        # 切到哪个角色就回放哪个角色的历史
        with contextlib.suppress(Exception):
            await conn.send_history()

        log.info(
            "session %s persona -> %s (该角色已有 %d 条历史)",
            session.session_id[:8], pid, len(conn.session.history),
        )
        return True

    if mtype == "set_mode":
        mode = data.get("mode")
        personas = engines.personas
        if not mode or personas.mode(str(mode)) is None:
            await _send_json(
                ws,
                _err("bad_message", f"未知模式: {mode}（可用 free/task/drill）"),
            )
            return True
        session.mode_id = str(mode)
        await _send_json(ws, {"type": "mode_changed", "mode": session.mode_id})
        log.info("session %s mode -> %s", session.session_id[:8], mode)
        return True

    # ---- 直接送文本（跳过 STT）----
    if mtype == "text":
        text = (data.get("text") or "").strip()
        if not text:
            await _send_json(ws, _err("bad_message", "text 不能为空"))
            return True
        turn_id = session.next_turn_id()
        await _send_json(
            ws,
            {
                "type": "user_text",
                "turn_id": turn_id,
                "text": text,
                "latency": {"stt_ms": 0.0},
                "truncated": False,
            },
        )
        # 图片（多模态）：data URL 列表，最多 4 张，单张 <= 6MB
        raw_images = data.get("images") or []
        images: list[str] = []
        if isinstance(raw_images, list):
            for item in raw_images[:4]:
                if isinstance(item, str) and item.startswith("data:image/"):
                    if len(item) <= 6 * 1024 * 1024:
                        images.append(item)

        # 持久化 user 消息；图片转存成文件，JSON 里只留 URL
        _img_url = None
        _store = getattr(ws.app.state, "conversation_store", None)
        if images and _store is not None:
            _img_url = _store.save_image(images[0])
        conn.record("user", text, image=_img_url)  # noqa: SLF001
        conn._pending_image = None  # noqa: SLF001

        task = asyncio.create_task(
            conn.run_turn(text, turn_id=turn_id, stt_ms=0.0, images=images),
            name="turn-text",
        )
        conn._turn_task = task  # noqa: SLF001 - 同模块协作
        return True

    # ---- 润色建议（显式请求）----
    if mtype == "suggestion":
        text = (data.get("text") or "").strip() or (session.last_user_text or "").strip()
        if not text:
            await _send_json(ws, _err("bad_message", "没有可用于分析的句子"))
            return True
        asyncio.create_task(
            _send_suggestions(conn, text, session.next_turn_id()), name="suggestion"
        )
        return True

    await _send_json(ws, _err("bad_message", f"未知消息类型: {mtype!r}"))
    return True


async def _send_suggestions(conn: _Connection, text: str, turn_id: int) -> None:
    """后台跑润色建议，失败静默。"""
    try:
        items = await conn.engines.suggestions.suggest(
            text,
            persona_id=conn.session.persona_id if conn.session else None,
            history=conn.session.history if conn.session else None,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 建议失败绝不影响对话
        log.debug("suggestion failed: %s", exc)
        return

    payload_items = [
        {
            "original": getattr(i, "original", ""),
            "better": getattr(i, "better", ""),
            "why": getattr(i, "why", ""),
            "register": getattr(i, "register", "neutral"),
        }
        for i in items
    ] if items else []
    if not payload_items:
        return
    await _send_json(
        conn.ws, {"type": "suggestion", "turn_id": turn_id, "items": payload_items}
    )


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    conn = _Connection(ws)
    peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
    log.info("ws connected from %s", peer)

    try:
        # ---- 等 hello ----
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=HELLO_TIMEOUT_S)
        except asyncio.TimeoutError:
            await _send_json(ws, _err("bad_message", "握手超时：未收到 hello", fatal=True))
            await ws.close(code=1002)
            return

        if first.get("type") == "websocket.disconnect":
            return

        raw = first.get("text")
        if raw is None:
            await _send_json(ws, _err("bad_message", "第一条消息必须是 hello（JSON）", fatal=True))
            await ws.close(code=1002)
            return

        keep = await _handle_text_frame(conn, raw)
        if not keep:
            await ws.close(code=1008)
            return

        # ---- 主循环 ----
        while not conn._closed:  # noqa: SLF001 - 同模块协作
            try:
                msg = await ws.receive()
            except WebSocketDisconnect:
                break
            except RuntimeError:
                break

            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                break

            data = msg.get("bytes")
            if data is not None:
                await conn.feed_audio(bytes(data))
                continue

            text = msg.get("text")
            if text is not None:
                keep_going = await _handle_text_frame(conn, text)
                if not keep_going:
                    await ws.close(code=1008)
                    break

    except WebSocketDisconnect:
        log.debug("ws disconnected peer=%s", peer)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 兜底，防止连接泄漏
        log.exception("ws loop crashed")
        with contextlib.suppress(Exception):
            await _send_json(conn.ws, _err("internal", "服务端内部错误"))
    finally:
        await conn.cleanup()
        if conn.session is not None:
            store: SessionStore | None = getattr(ws.app.state, "session_store", None)
            if store is not None:
                store.drop(conn.session.session_id)
        with contextlib.suppress(Exception):
            await ws.close()
        log.info("ws closed peer=%s", peer)


__all__ = ["router"]
