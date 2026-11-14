"""并发压测：按真实客户端的请求序列模拟 N 个在线用户，量出「每个动作花服务器多少 CPU」。

场景（--scenario）
    idle   只挂着事件长连接，什么都不做 → 每个在线用户的「待机成本」
    chat   文字聊天：发消息 → 拉消息 → 事件/有限轮询等音频 → 解码验证 → 标记已读
    voice  语音消息：上传 webm → 服务器解码 + STT → 同上
    feed   刷动态：首屏快照 + 翻一页
    call   实时通话：20ms PCM 帧上行 → 流式识别 → 逐句语音下行
    mix    按 --mix 的权重混合（默认 70% 聊天 / 10% 语音 / 20% 刷动态），另加 --calls 路通话

    python scripts/perf/loadgen.py --base http://127.0.0.1:8011 --users-file .tmp/perf/data/users.json \
        --scenario chat --users 100 --duration 60 --think 20

输出：服务器 CPU 核数占用、RSS、事件循环卡顿、各动作延迟分位、语音降级成文字的次数、
上游（假 Fish）的峰值并发与 429 次数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
import uuid
import io
import wave
import math
from collections import defaultdict

import httpx
import websockets

from perf_utils import source_provenance


def audio_is_decodable(data: bytes) -> bool:
    """Verify PCM WAV without a network codec or accepting header-shaped junk.

    Other formats require an actual ffmpeg decode. A missing decoder is reported
    as unverified audio rather than counted as playable.
    """
    if data.startswith(b"RIFF"):
        try:
            with wave.open(io.BytesIO(data), "rb") as audio:
                frames, channels, width = audio.getnframes(), audio.getnchannels(), audio.getsampwidth()
                return (frames > 0 and audio.getframerate() > 0 and
                        len(audio.readframes(frames)) == frames * channels * width)
        except (EOFError, wave.Error, ValueError):
            return False
    try:
        decoded = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
            "-i", "pipe:0", "-f", "s16le", "-ac", "1", "pipe:1"],
            input=data, capture_output=True, timeout=10, check=False)
        return decoded.returncode == 0 and len(decoded.stdout) > 0 and not decoded.stderr
    except (OSError, subprocess.TimeoutExpired):
        return False

TEXTS = (
    "I had a pretty long day, honestly. How was yours?",
    "Do you ever just feel like doing nothing on a Sunday?",
    "I am trying to cook more at home this month.",
    "What would you do if you had a free afternoon?",
    "I finally finished that book you mentioned.",
)


class Metrics:
    def __init__(self) -> None:
        self.latency: dict[str, list[float]] = defaultdict(list)
        self.count: dict[str, int] = defaultdict(int)
        self.recording = False

    def observe(self, name: str, seconds: float) -> None:
        if self.recording:
            self.latency[name].append(seconds)

    def add(self, name: str, value: int = 1) -> None:
        if self.recording:
            self.count[name] += value

    def summary(self) -> dict:
        out: dict = {"counts": dict(sorted(self.count.items())), "latency_ms": {}}
        for name, values in sorted(self.latency.items()):
            data = sorted(values)
            pick = lambda q: round(data[min(len(data) - 1, int(len(data) * q))] * 1000, 1)  # noqa: E731
            out["latency_ms"][name] = {"n": len(data), "p50": pick(0.5), "p95": pick(0.95),
                                       "p99": pick(0.99), "max": round(data[-1] * 1000, 1)}
        return out


def make_webm(seconds: float = 3.0) -> bytes:
    """用 ffmpeg 现做一段 webm/opus（浏览器 MediaRecorder 的真实上传格式）；没有 ffmpeg 就退回 wav。"""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             f"anoisesrc=d={seconds}:c=pink:r=16000:a=0.2", "-ac", "1", "-c:a", "libopus", "-b:a", "24k",
             "-f", "webm", "-"],
            capture_output=True, timeout=30, check=False)
        if proc.returncode == 0 and len(proc.stdout) > 1000:
            return proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return b""


def make_wav(seconds: float = 3.0) -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    rng = random.Random(7)
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(bytes(rng.getrandbits(8) for _ in range(int(16000 * seconds) * 2)))
    return buffer.getvalue()


class VUser:
    """一个虚拟用户：请求序列对齐 frontend/src/w4 的真实行为。"""

    def __init__(self, cfg, user: dict, client: httpx.AsyncClient, metrics: Metrics, audio: dict) -> None:
        self.cfg = cfg
        self.user = user
        self.client = client
        self.metrics = metrics
        self.audio = audio
        self.headers = {"X-Session-Token": user["token"]}
        if cfg.no_keepalive:
            self.headers["Connection"] = "close"
        self.rng = random.Random(user["user_id"])
        self.current_friend = ""
        self.sequence = 0
        self._refresh: asyncio.Task | None = None
        self._audio_changed = asyncio.Event()
        self._latest_messages: dict[str, list[dict]] = {}
        self._pending_events: list[dict] = []

    async def request(self, name: str, method: str, path: str, *, keep_errors=False, **kwargs) -> httpx.Response | None:
        started = time.perf_counter()
        try:
            response = await self.client.request(method, path, headers=self.headers, **kwargs)
        except httpx.HTTPError as exc:
            self.metrics.add(f"{name}.error.{type(exc).__name__}")
            return None
        self.metrics.observe(name, time.perf_counter() - started)
        self.metrics.add("http.requests")
        self.metrics.add("net.bytes_down", len(response.content))
        if response.status_code >= 400:
            self.metrics.add(f"{name}.http_{response.status_code}")
            return response if keep_errors else None
        return response

    # ------------------------------------------------------------ 事件长连接
    async def events(self) -> None:
        ws_base = self.cfg.base.replace("http://", "ws://").replace("https://", "wss://")
        while True:
            ticket = await self.request("event_ticket", "POST", "/api/v3/client/event-ticket")
            if ticket is None:
                await asyncio.sleep(2)
                continue
            data = ticket.json()
            self.sequence = self.sequence or int(data.get("current_sequence") or 0)
            try:
                async with websockets.connect(ws_base + data["ws_path"], max_size=None) as ws:
                    await ws.send(json.dumps({"type": "auth", "ticket": data["ticket"],
                                              "last_sequence": self.sequence or None}))
                    self.metrics.add("events.connected")
                    async for raw in ws:
                        event = json.loads(raw)
                        if "id" not in event:
                            continue
                        self.sequence = max(self.sequence, int(event.get("sequence") or 0))
                        self.metrics.add("events.received")
                        created = event.get("payload", {}).get("_sent_at")
                        if created:
                            self.metrics.observe("event_delivery", time.time() - float(created))
                        self._pending_events.append(event)
                        if self._refresh is None or self._refresh.done():
                            self._refresh = asyncio.create_task(self._refresh_after_event(event))
            except (OSError, websockets.WebSocketException):
                self.metrics.add("events.disconnected")
                await asyncio.sleep(1)

    async def _refresh_after_event(self, event: dict) -> None:
        await asyncio.sleep(0.3)
        events, self._pending_events = self._pending_events or [event], []
        friend_types = {"message", "new_message", "friend_request", "friend_accepted",
                        "friend_removed", "request_accepted", "request_declined"}
        thread_types = {"message", "new_message", "audio_ready", "audio_failed"}
        if any(item.get("type") in friend_types for item in events):
            await self.request("friends", "GET", "/api/v2/friends?limit=50")
        friend_id = self.current_friend
        if friend_id and any(item.get("type") in thread_types and item.get("friend_id") == friend_id for item in events):
            result = await self.request("messages", "GET", f"/api/v2/friends/{friend_id}/messages?limit=30")
            if result is not None:
                self._latest_messages[friend_id] = result.json().get("messages") or []
                self._audio_changed.set()
        if self._pending_events:
            self._refresh = asyncio.create_task(self._refresh_after_event(self._pending_events[0]))

    # ------------------------------------------------------------ 动作
    async def _after_turn(self, name: str, friend_id: str, response: httpx.Response | None, started: float) -> None:
        if response is None:
            self.metrics.add(f"{name}.failed")
            self.metrics.add("turns.failed")
            return
        if response.status_code >= 400:
            self.metrics.add("turns.rejected" if response.status_code == 429 else "turns.failed")
            return
        self.metrics.observe(f"{name}.total", time.perf_counter() - started)
        self.metrics.add(f"{name}.ok")
        self.metrics.add("turns.completed")
        body = response.json()
        friend_messages = body.get("friend_messages") or []
        reply_id = str((friend_messages[0] or {}).get("id") or "") if friend_messages else ""
        deadline = started + getattr(self.cfg, "audio_deadline", 30.0)
        # sendText/sendVoice both fetch the message list after the POST, even if
        # the response already includes a ready audio URL.
        fetched = await self.request("messages", "GET", f"/api/v2/friends/{friend_id}/messages?limit=20",
                                     timeout=max(.01, deadline-time.perf_counter()))
        messages = fetched.json().get("messages", []) if fetched is not None else friend_messages
        delay = getattr(self.cfg, "poll_initial", 2.0)
        playable = False
        while True:
            reply = next((m for m in messages if reply_id and str(m.get("id")) == reply_id), None)
            if reply is None and not reply_id:
                # Missing response IDs cannot safely attribute an old message to
                # the offered turn; count missing audio instead of stale success.
                break
            if reply and reply.get("audio_url"):
                got = await self.request("audio_get", "GET", reply["audio_url"],
                    timeout=max(.01, deadline-time.perf_counter()))
                if got is not None:
                    self.metrics.add("net.audio_bytes", len(got.content))
                    playable = await asyncio.to_thread(audio_is_decodable, got.content)
                    if not playable:
                        self.metrics.add("audio.invalid_or_unverified")
                if playable:
                    elapsed = time.perf_counter() - started
                    self.metrics.add("turns.playable_audio")
                    self.metrics.add("turns.audio_within_deadline" if elapsed <= getattr(self.cfg, "audio_deadline", 30.0) else "turns.audio_late")
                    self.metrics.observe("time_to_playable", elapsed)
                break
            if (reply and reply.get("audio_status") == "failed") or time.perf_counter() >= deadline:
                break
            self._audio_changed.clear()
            try:
                await asyncio.wait_for(self._audio_changed.wait(), min(delay, max(.001, deadline-time.perf_counter())))
                messages = self._latest_messages.get(friend_id, [])
            except TimeoutError:
                fetched = await self.request("messages", "GET", f"/api/v2/friends/{friend_id}/messages?limit=30",
                                             timeout=max(.01, deadline-time.perf_counter()))
                messages = fetched.json().get("messages", []) if fetched is not None else []
                delay = min(8.0, delay * 2)
        if not playable:
            self.metrics.add("turns.missing_audio")
            self.metrics.add(f"{name}.reply_without_audio")
        await self.request("mark_read", "POST", f"/api/v2/friends/{friend_id}/read")

    async def text_turn(self) -> None:
        self.metrics.add("turns.offered")
        friend_id = self.current_friend = self.rng.choice(self.user["friends"])
        started = time.perf_counter()
        response = await self.request(
            "text_turn", "POST", f"/api/v2/chat/{friend_id}/text",
            json={"text": self.rng.choice(TEXTS), "client_message_id": uuid.uuid4().hex},
            timeout=getattr(self.cfg, "audio_deadline", 30.0) + 5, keep_errors=True)
        await self._after_turn("text_turn", friend_id, response, started)

    async def voice_turn(self) -> None:
        self.metrics.add("turns.offered")
        friend_id = self.current_friend = self.rng.choice(self.user["friends"])
        started = time.perf_counter()
        response = await self.request(
            "voice_turn", "POST", f"/api/v2/chat/{friend_id}/voice",
            files={"audio": (self.audio["name"], self.audio["data"], self.audio["mime"])},
            data={"duration_ms": "3000"}, timeout=getattr(self.cfg, "audio_deadline", 30.0) + 5,
            keep_errors=True)
        self.metrics.add("net.bytes_up", len(self.audio["data"]))
        await self._after_turn("voice_turn", friend_id, response, started)

    async def feed_browse(self) -> None:
        first = await self.request("feed_page", "GET", "/api/v3/feed?limit=10")
        await self.request("feed_summary", "GET", "/api/v3/feed/summary")
        cursor = (first.json().get("next_cursor") if first is not None else "") or ""
        if cursor:
            await asyncio.sleep(self.rng.uniform(1.0, 3.0))
            await self.request("feed_page", "GET", f"/api/v3/feed?limit=10&cursor={cursor}")

    async def call(self) -> None:
        """一路通话：持续到任务被取消。上行按原生端的 20ms/640B 帧（50 帧/秒，只在说话时发）。"""
        friend_id = self.rng.choice(self.user["friends"])
        created = await self.request("call_create", "POST", "/api/v3/calls",
                                     json={"friend_id": friend_id, "request_id": uuid.uuid4().hex})
        if created is None:
            self.metrics.add("call.rejected")
            return
        data = created.json()
        ws_base = self.cfg.base.replace("http://", "ws://").replace("https://", "wss://")
        frame = bytes(self.rng.getrandbits(8) for _ in range(640))
        turn_done = asyncio.Event()
        state = {"turn_started": 0.0, "first_audio": False}
        try:
            async with websockets.connect(ws_base + data["ws_path"], max_size=None) as ws:
                await ws.send(json.dumps({"type": "auth", "ticket": data["ticket"]}))

                async def reader() -> None:
                    async for raw in ws:
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind == "ready":
                            self.metrics.add("call.connected")
                        elif kind == "audio":
                            if not state["first_audio"]:
                                state["first_audio"] = True
                                self.metrics.observe("call.first_audio", time.perf_counter() - state["turn_started"])
                            got = await self.request("call_audio_get", "GET", event["audio_url"])
                            if got is not None:
                                self.metrics.add("net.audio_bytes", len(got.content))
                            # 模拟播放完才回执（决定一路通话占用 TTS 的占空比）
                            await asyncio.sleep(min(self.cfg.playback_cap, float(event.get("duration_ms") or 0) / 1000.0))
                            await ws.send(json.dumps({"type": "playback_done", "turn_id": event.get("turn_id"),
                                                      "audio_id": event.get("audio_id")}))
                            self.metrics.add("call.sentences")
                        elif kind == "turn_done":
                            self.metrics.add("call.turns_ok")
                            turn_done.set()
                        elif kind == "error":
                            self.metrics.add(f"call.error.{event.get('code')}")
                            turn_done.set()

                read_task = asyncio.create_task(reader())
                try:
                    await asyncio.sleep(0.5)
                    while True:
                        speak_s = self.rng.uniform(2.5, 5.0)
                        started = time.perf_counter()
                        sent = 0
                        while True:  # 按真实时间补帧：Windows 定时器粒度粗，不能指望 sleep(0.02) 准点
                            elapsed = time.perf_counter() - started
                            due = min(int(speak_s / 0.02), int(elapsed / 0.02) + 1)
                            while sent < due:
                                await ws.send(frame)
                                sent += 1
                            if sent >= int(speak_s / 0.02):
                                break
                            await asyncio.sleep(0.01)
                        self.metrics.add("call.frames_up", sent)
                        self.metrics.add("net.bytes_up", sent * 640)
                        turn_done.clear()
                        state["turn_started"], state["first_audio"] = time.perf_counter(), False
                        await ws.send(json.dumps({"type": "utterance_end", "turn_id": uuid.uuid4().hex}))
                        try:
                            await asyncio.wait_for(turn_done.wait(), 45)
                            self.metrics.observe("call.turn", time.perf_counter() - state["turn_started"])
                        except asyncio.TimeoutError:
                            self.metrics.add("call.turn_timeout")
                        await asyncio.sleep(self.rng.uniform(1.0, 3.0))
                finally:
                    read_task.cancel()
                    try:
                        await ws.send(json.dumps({"type": "end"}))
                    except Exception:  # noqa: BLE001
                        pass
        except (OSError, websockets.WebSocketException) as exc:
            self.metrics.add(f"call.ws_error.{type(exc).__name__}")


async def user_main(vu: VUser, cfg, delay: float, weights: dict[str, float]) -> None:
    await asyncio.sleep(delay)
    await vu.request("bootstrap", "GET", "/api/v3/client/bootstrap")
    events = None if cfg.no_events else asyncio.create_task(vu.events())
    actions = {"chat": vu.text_turn, "voice": vu.voice_turn, "feed": vu.feed_browse}
    names = [n for n, w in weights.items() if w > 0]
    try:
        if not names:
            await asyncio.Event().wait()
        # 首个动作在 [0, think) 内随机错开，避免所有人同一秒发消息
        await asyncio.sleep(vu.rng.uniform(0, cfg.think))
        while True:
            name = vu.rng.choices(names, weights=[weights[n] for n in names])[0]
            await actions[name]()
            await asyncio.sleep(vu.rng.uniform(0.5 * cfg.think, 1.5 * cfg.think))
    finally:
        if events is not None:
            events.cancel()


async def call_main(vu: VUser, delay: float) -> None:
    await asyncio.sleep(delay)
    while True:
        await vu.call()
        await asyncio.sleep(2)


async def schedule_arrivals(action, metrics: Metrics, *, duration: float, rate: float,
                            burst: int, max_inflight: int, drain_timeout: float) -> dict:
    """Open-loop arrivals never wait for completions or create an unbounded queue."""
    start = time.perf_counter()
    count = burst if burst else max(0, math.ceil(duration * rate - 1e-9))
    active: set[asyncio.Task] = set()

    async def execute(index):
        try:
            await action(index)
        except asyncio.CancelledError:
            metrics.add("arrivals.cancelled")
            raise
        except Exception as exc:  # report worker failures without aborting offered load
            metrics.add("arrivals.failed")
            metrics.add(f"arrivals.error.{type(exc).__name__}")

    for index in range(count):
        target = start if burst else start + index / rate
        await asyncio.sleep(max(0, target - time.perf_counter()))
        metrics.add("arrivals.offered")
        metrics.observe("arrival_lag", max(0, time.perf_counter() - target))
        active = {task for task in active if not task.done()}
        if len(active) >= max(1, max_inflight):
            metrics.add("arrivals.client_dropped")
            continue
        metrics.add("arrivals.started")
        task = asyncio.create_task(execute(index))
        active.add(task)
    await asyncio.sleep(max(0, start + duration - time.perf_counter()))
    offering_wall = time.perf_counter() - start
    if active:
        _done, pending = await asyncio.wait(active, timeout=drain_timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
    return {"offering_wall_s": offering_wall, "drain_s": time.perf_counter() - start - offering_wall}


async def run(cfg) -> dict:
    with open(cfg.users_file, encoding="utf-8") as fh:
        users = json.load(fh)
    need = cfg.users + cfg.calls
    if len(users) < need:
        raise SystemExit(f"users.json 只有 {len(users)} 个用户，不够 {need} 个；重新跑 seed_bench_data.py --users {need}")

    presets = {"idle": {}, "chat": {"chat": 1}, "voice": {"voice": 1}, "feed": {"feed": 1}, "call": {}}
    if cfg.scenario == "mix":
        weights = {k: float(v) for k, v in (item.split("=") for item in cfg.mix.split(","))}
    else:
        weights = presets[cfg.scenario]

    webm = make_webm() if weights.get("voice") else b""
    audio = ({"name": "voice.webm", "data": webm, "mime": "audio/webm"} if webm
             else {"name": "voice.wav", "data": make_wav(), "mime": "audio/wav"})
    metrics = Metrics()
    limits = httpx.Limits(max_connections=4000, max_keepalive_connections=0 if cfg.no_keepalive else 1000)
    perf = httpx.AsyncClient(timeout=30)

    async def fetch(url: str, method: str = "GET") -> dict:
        try:
            return (await perf.request(method, url)).json()
        except Exception:  # noqa: BLE001 - 观测端点不可用时仍输出客户端侧数据
            return {}

    async with httpx.AsyncClient(base_url=cfg.base, timeout=60, limits=limits) as client:
        tasks = []
        vus = [VUser(cfg, users[index], client, metrics, audio) for index in range(cfg.users)]
        open_loop = cfg.mode != "closed"
        busy_users: set[int] = set()
        for index in range(cfg.users):
            vu = vus[index]
            if open_loop:
                await vu.request("bootstrap", "GET", "/api/v3/client/bootstrap")
                if not cfg.no_events:
                    tasks.append(asyncio.create_task(vu.events()))
            else:
                tasks.append(asyncio.create_task(user_main(vu, cfg, cfg.ramp * index / max(1, cfg.users), weights)))
        for index in range(cfg.calls):
            vu = VUser(cfg, users[cfg.users + index], client, metrics, audio)
            tasks.append(asyncio.create_task(call_main(vu, cfg.ramp * index / max(1, cfg.calls))))

        await asyncio.sleep(cfg.warmup + (0 if open_loop else cfg.ramp))
        await fetch(cfg.base + "/__perf/reset", "POST")
        if cfg.fake:
            await fetch(cfg.fake + "/__reset", "POST")
        before = await fetch(cfg.base + "/__perf/stats")
        metrics.recording = True
        provenance = source_provenance()
        started = time.perf_counter()
        if open_loop:
            action_names = [name for name, weight in weights.items() if weight > 0]
            if not action_names:
                raise ValueError("Arrival/burst mode requires chat, voice, feed or mix actions")
            async def offer(index):
                user_index = index % len(vus)
                if user_index in busy_users:
                    metrics.add("arrivals.user_busy")
                    return
                busy_users.add(user_index)
                vu = vus[user_index]
                name = vu.rng.choices(action_names, weights=[weights[n] for n in action_names])[0]
                try:
                    await {"chat": vu.text_turn, "voice": vu.voice_turn, "feed": vu.feed_browse}[name]()
                finally:
                    busy_users.discard(user_index)
            timing = await schedule_arrivals(offer, metrics, duration=cfg.duration, rate=cfg.rate,
                burst=(cfg.burst or cfg.users) if cfg.mode == "burst" else 0,
                max_inflight=cfg.max_inflight, drain_timeout=cfg.drain_timeout)
        else:
            await asyncio.sleep(cfg.duration)
            timing = {"offering_wall_s": time.perf_counter() - started, "drain_s": 0}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        metrics.recording = False
        wall = time.perf_counter() - started
        after = await fetch(cfg.base + "/__perf/stats")
        upstream = await fetch(cfg.fake + "/__stats") if cfg.fake else {}
        profile = await fetch(cfg.base + "/__perf/profile") if cfg.profile else {}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        refreshes = [vu._refresh for vu in vus if vu._refresh and not vu._refresh.done()]
        for task in refreshes:
            task.cancel()
        await asyncio.gather(*refreshes, return_exceptions=True)
    await perf.aclose()

    summary = metrics.summary()
    counts = summary["counts"]
    cpu = (after.get("cpu_s", 0) - before.get("cpu_s", 0)) if after and before else None
    turns = counts.get("text_turn.ok", 0) + counts.get("voice_turn.ok", 0)
    result = {
        "label": cfg.label, "scenario": cfg.scenario, "users": cfg.users, "calls": cfg.calls,
        "think_s": cfg.think, "duration_s": round(wall, 1),
        "offering_duration_s": round(timing["offering_wall_s"], 4),
        "requested_duration_s": cfg.duration, "drain_s": round(timing["drain_s"], 4), "mode": cfg.mode,
        "configured_arrival_rate": cfg.rate if cfg.mode == "arrival" else None,
        "provenance": provenance,
        "configuration": {"max_inflight": cfg.max_inflight, "audio_deadline_s": cfg.audio_deadline,
            "events": not cfg.no_events, "keepalive": not cfg.no_keepalive,
            "warmup_s": cfg.warmup, "drain_timeout_s": cfg.drain_timeout,
            "server": after.get("configuration", {}), "fake": bool(cfg.fake),
            "fake_upstream": upstream.get("configuration", {})},
        "server": {
            "cpu_s": None if cpu is None else round(cpu, 3),
            "cores_used": None if cpu is None else round(cpu / wall, 4),
            "rss_mb": after.get("rss_mb"), "peak_rss_mb": after.get("peak_rss_mb"),
            "threads": after.get("threads"), "tasks": after.get("tasks"), "loop": after.get("loop"),
            "lag": after.get("lag"),
            "resources_before": before.get("resources"),
            "resources_after": after.get("resources"),
            "provider_pool_before": before.get("provider_pool"),
            "provider_pool_after": after.get("provider_pool"),
        },
        "rates": {
            "turns_per_s": round(turns / wall, 3),
            "actual_offered_per_s": round(counts.get("arrivals.offered", counts.get("turns.offered", 0)) / timing["offering_wall_s"], 3),
            "completed_per_s_including_drain": round(counts.get("turns.completed", 0) / wall, 3),
            "http_rps": round(counts.get("http.requests", 0) / wall, 2),
            "down_kbps": round(counts.get("net.bytes_down", 0) * 8 / 1000 / wall, 1),
            "up_kbps": round(counts.get("net.bytes_up", 0) * 8 / 1000 / wall, 1),
            "cpu_ms_per_turn": None if not (cpu and turns) else round(cpu * 1000 / turns, 2),
        },
        "upstream": upstream,
        **summary,
    }
    if profile.get("text"):
        result["profile_file"] = profile.get("file")
        print(profile["text"])
    offered = counts.get("arrivals.offered", counts.get("turns.offered", 0))
    result["acceptance"] = {"offered": offered, "completed": counts.get("turns.completed", 0),
        "rejected": counts.get("turns.rejected", 0), "failed": counts.get("turns.failed", 0),
        "missing_audio": counts.get("turns.missing_audio", 0),
        "client_dropped": counts.get("arrivals.client_dropped", 0),
        "user_busy": counts.get("arrivals.user_busy", 0),
        "cancelled": counts.get("arrivals.cancelled", 0),
        "playable_audio_fraction_of_offered": counts.get("turns.playable_audio", 0) / offered if offered else None,
        "within_deadline_fraction_of_offered": counts.get("turns.audio_within_deadline", 0) / offered if offered else None}
    result["provenance_end"] = source_provenance()
    result["source_unchanged_during_measurement"] = result["provenance"] == result["provenance_end"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", default="http://127.0.0.1:8011")
    parser.add_argument("--fake", default="http://127.0.0.1:9100", help="fake_upstreams 地址（取上游统计；留空=不取）")
    parser.add_argument("--users-file", required=True)
    parser.add_argument("--scenario", choices=("idle", "chat", "voice", "feed", "call", "mix"), default="chat")
    parser.add_argument("--mix", default="chat=0.7,voice=0.1,feed=0.2")
    parser.add_argument("--users", type=int, default=100, help="在线用户数（都挂事件长连接）")
    parser.add_argument("--calls", type=int, default=0, help="额外的同时通话路数")
    parser.add_argument("--think", type=float, default=20.0, help="两次动作之间的平均间隔（秒）")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--mode", choices=("closed", "arrival", "burst"), default="arrival",
                        help="arrival/burst offer independently of backend latency; closed is completion-paced")
    parser.add_argument("--rate", type=float, default=3.3333333333, help="Fixed total arrivals per second")
    parser.add_argument("--burst", type=int, default=0, help="Simultaneous offered actions (0=users)")
    parser.add_argument("--max-inflight", type=int, default=200, help="Maximum client tasks; excess offered arrivals counted as client_dropped")
    parser.add_argument("--drain-timeout", type=float, default=35)
    parser.add_argument("--audio-deadline", type=float, default=30)
    parser.add_argument("--poll-initial", type=float, default=2)
    parser.add_argument("--ramp", type=float, default=8.0)
    parser.add_argument("--warmup", type=float, default=6.0)
    parser.add_argument("--playback-cap", type=float, default=6.0, help="通话里每句语音最多模拟播放几秒")
    parser.add_argument("--no-events", action="store_true", help="不建事件长连接")
    parser.add_argument("--no-keepalive", action="store_true", help="每个请求新建连接（模拟 nginx 未开上游 keepalive）")
    parser.add_argument("--profile", action="store_true", help="结束时取服务端 cProfile 报告")
    parser.add_argument("--label", default="")
    parser.add_argument("--out", default="", help="把结果追加到该 jsonl 文件")
    cfg = parser.parse_args()
    if cfg.duration <= 0 or cfg.rate <= 0 or cfg.users < 1 or cfg.max_inflight < 1:
        parser.error("duration, rate, users and max-inflight must be positive")
    cfg.base = cfg.base.rstrip("/")
    cfg.fake = cfg.fake.rstrip("/")
    result = asyncio.run(run(cfg))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if cfg.out:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.out)), exist_ok=True)
        with open(cfg.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
