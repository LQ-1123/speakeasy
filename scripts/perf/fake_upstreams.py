"""压测用的「假上游」：一个进程同时扮演 LLM / Fish TTS / 硅基流动 STT / Deepgram。

为什么需要
----------
线上一轮对话 90% 的时间在等上游（LLM 首字、TTS 合成）。压测如果真打上游，
测到的是上游的限流和账单，而不是这台服务器自己的承载力。这里用可配置的
延迟把上游的「形状」模拟出来（首字延迟、吐字速度、合成耗时、并发上限），
后端照常走真实的 HTTP/SSE/WebSocket 代码路径，CPU 花在哪里一目了然。

    python scripts/perf/fake_upstreams.py --port 9100 --fish-concurrency 5

端点（与真实上游同路径，后端只需改 base_url）
    POST /v1/chat/completions        OpenAI 兼容（流式 SSE / 非流式）
    POST /v1/tts, GET /model         Fish Audio（超过并发上限回 429，和真实行为一致）
    POST /v1/audio/transcriptions    SiliconFlow SenseVoice
    WS   /v1/listen                  Deepgram 流式识别
    GET  /__stats                    各上游的请求数 / 峰值并发 / 429 次数
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import io
import json
import time
import wave

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

REPLIES = (
    "Yeah, that actually sounds pretty good to me. I was out walking earlier and it was "
    "freezing, honestly. What did you end up doing after that?",
    "Oh nice. I had a slow morning, just coffee and a bit of reading. "
    "The afternoon got busy though. How was yours?",
    "Ha, fair enough. I keep telling myself I will cook tonight, but we both know how that ends. "
    "Did you eat already?",
    "That makes sense. Honestly I would have done the same thing. "
    "It is not worth the stress, right?",
)


def build_app(cfg: argparse.Namespace) -> FastAPI:
    app = FastAPI()
    stats = {
        "llm": {"requests": 0, "inflight": 0, "peak": 0},
        "fish": {"requests": 0, "inflight": 0, "peak": 0, "rejected_429": 0},
        "stt": {"requests": 0, "inflight": 0, "peak": 0},
        "deepgram": {"sessions": 0, "inflight": 0, "peak": 0, "audio_bytes": 0, "frames": 0},
    }
    counter = itertools.count()
    account_labels = {}
    fish_accounts = {}
    if getattr(cfg, "fish_per_account", False):
        stats["fish"]["accounts"] = fish_accounts

    def fish_account(authorization):
        if authorization not in account_labels:
            account_labels[authorization] = f"account-{len(account_labels) + 1:04d}"
        label = account_labels[authorization]
        return fish_accounts.setdefault(label, {"requests": 0, "inflight": 0, "peak": 0, "rejected_429": 0})

    def enter(name: str) -> None:
        item = stats[name]
        item["inflight"] += 1
        item["peak"] = max(item["peak"], item["inflight"])

    def leave(name: str) -> None:
        stats[name]["inflight"] -= 1

    @app.get("/__stats")
    async def get_stats() -> dict:
        return {**stats, "configuration": vars(cfg)}

    @app.post("/__reset")
    async def reset_stats() -> dict:
        for item in stats.values():
            for key in item:
                if key == "accounts":
                    for account in item[key].values():
                        for field in account:
                            if field != "inflight":
                                account[field] = account["inflight"] if field == "peak" else 0
                elif key != "inflight":
                    item[key] = 0
        return {"ok": True}

    # ------------------------------------------------------------------ LLM
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        stats["llm"]["requests"] += 1
        request_number = stats["llm"]["requests"]
        for option, status in (("llm_429_every", 429), ("llm_error_every", 503)):
            every = getattr(cfg, option, 0)
            if every and request_number % every == 0:
                stats["llm"][f"rejected_{status}"] = stats["llm"].get(f"rejected_{status}", 0) + 1
                return JSONResponse({"error": {"message": "injected fake upstream error"}},
                                    status_code=status, headers={"Retry-After": "1"})
        if getattr(cfg, "llm_slow_every", 0) and request_number % cfg.llm_slow_every == 0:
            await asyncio.sleep(cfg.llm_slow_seconds)
        text = REPLIES[next(counter) % len(REPLIES)]
        created = int(time.time())
        if not body.get("stream"):
            # 结构化补全（润色 / 记忆 / 评论）：给一个合法但空的 JSON，后端走各自的兜底
            enter("llm")
            try:
                await asyncio.sleep(cfg.llm_json_latency)
            finally:
                leave("llm")
            return {
                "id": "chatcmpl-fake", "object": "chat.completion", "created": created,
                "model": body.get("model", "fake"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "{}"}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
            }

        # 约 4 字符一个 token，按 llm_tps 匀速吐
        pieces = [text[i:i + 4] for i in range(0, len(text), 4)]

        async def sse():
            enter("llm")
            try:
                await asyncio.sleep(cfg.llm_ttft)
                for piece in pieces:
                    chunk = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                             "created": created, "model": body.get("model", "fake"),
                             "choices": [{"index": 0, "delta": {"content": piece},
                                          "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(1.0 / cfg.llm_tps)
                done = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                        "created": created, "model": body.get("model", "fake"),
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n"
            finally:
                leave("llm")

        return StreamingResponse(sse(), media_type="text/event-stream")

    # ------------------------------------------------------------------ Fish
    @app.post("/v1/tts")
    async def fish_tts(request: Request):
        body = await request.json()
        fish = stats["fish"]
        fish["requests"] += 1
        account = fish_account(request.headers.get("authorization", "")) if getattr(cfg, "fish_per_account", False) else None
        if account is not None:
            account["requests"] += 1
        if str(body.get("format") or "wav").lower() != "wav":
            return JSONResponse({"error": "Fake TTS supports format=wav only; no mislabeled audio"}, status_code=422)
        account_limit = getattr(cfg, "fish_account_concurrency", 5)
        if ((cfg.fish_concurrency > 0 and fish["inflight"] >= cfg.fish_concurrency)
                or (account is not None and account["inflight"] >= account_limit)):
            fish["rejected_429"] += 1
            if account is not None:
                account["rejected_429"] += 1
            return JSONResponse({"status": 429, "message": "Too many concurrent requests"},
                                status_code=429)
        enter("fish")
        if account is not None:
            account["inflight"] += 1
            account["peak"] = max(account["peak"], account["inflight"])
        try:
            text = str(body.get("text") or "")
            # 合成耗时 ≈ 固定开销 + 按字数增长；音频大小按码率 × 估算时长
            await asyncio.sleep(cfg.fish_base + len(text) * cfg.fish_per_char)
            seconds = max(0.8, len(text) / 15.0)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(bytes(int(seconds * 24000) * 2))
        finally:
            leave("fish")
            if account is not None:
                account["inflight"] -= 1
        return Response(content=buffer.getvalue(), media_type="audio/wav")

    @app.get("/model")
    async def fish_models(page_size: int = 100, page_number: int = 1):
        return {"total": 0, "items": []}

    # ------------------------------------------------------------------ STT
    @app.post("/v1/audio/transcriptions")
    async def stt(request: Request):
        await request.body()
        stats["stt"]["requests"] += 1
        enter("stt")
        try:
            await asyncio.sleep(cfg.stt_latency)
        finally:
            leave("stt")
        return {"text": "I went for a walk and then grabbed a coffee near the station."}

    # ------------------------------------------------------------------ Deepgram
    @app.websocket("/v1/listen")
    async def deepgram(ws: WebSocket):
        await ws.accept()
        dg = stats["deepgram"]
        dg["sessions"] += 1
        enter("deepgram")
        seconds = 0.0
        since_interim = 0.0
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                audio = message.get("bytes")
                if audio:
                    dg["audio_bytes"] += len(audio)
                    dg["frames"] += 1
                    chunk_s = len(audio) / 32000.0
                    seconds += chunk_s
                    since_interim += chunk_s
                    if since_interim >= 0.5:  # 每 0.5 秒语音回一个中间结果
                        since_interim = 0.0
                        await ws.send_text(json.dumps({
                            "type": "Results", "is_final": False, "start": seconds, "duration": 0.5,
                            "channel": {"alternatives": [{"transcript": "I went for a walk and"}]},
                        }))
                    continue
                try:
                    command = json.loads(message.get("text") or "{}")
                except ValueError:
                    continue
                kind = command.get("type")
                if kind == "Finalize":
                    await asyncio.sleep(0.15)
                    await ws.send_text(json.dumps({
                        "type": "Results", "is_final": True, "from_finalize": True,
                        "start": seconds, "duration": 0.1,
                        "channel": {"alternatives": [{
                            "transcript": "I went for a walk and then grabbed a coffee."}]},
                    }))
                elif kind == "CloseStream":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            leave("deepgram")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--llm-ttft", type=float, default=0.6, help="LLM 首字延迟（秒）")
    parser.add_argument("--llm-tps", type=float, default=40.0, help="LLM 每秒吐多少个 token")
    parser.add_argument("--llm-json-latency", type=float, default=1.2, help="非流式补全耗时（秒）")
    parser.add_argument("--fish-base", type=float, default=1.0, help="Fish 单次合成固定耗时（秒）")
    parser.add_argument("--fish-per-char", type=float, default=0.012, help="Fish 每字符额外耗时（秒）")
    parser.add_argument("--fish-concurrency", type=int, default=5,
                        help="Synthetic concurrent-request cap, configurable independently of any real account (0=unlimited)")
    parser.add_argument("--fish-per-account", action="store_true",
                        help="Also enforce independent limits per Authorization; stats use anonymous account labels")
    parser.add_argument("--fish-account-concurrency", type=int, default=5)
    parser.add_argument("--llm-429-every", type=int, default=0)
    parser.add_argument("--llm-error-every", type=int, default=0)
    parser.add_argument("--llm-slow-every", type=int, default=0)
    parser.add_argument("--llm-slow-seconds", type=float, default=5)
    parser.add_argument("--stt-latency", type=float, default=0.4, help="整段转写耗时（秒）")
    cfg = parser.parse_args()
    uvicorn.run(build_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
