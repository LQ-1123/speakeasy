"""测试后端 /stt/stream WebSocket（本地后端 → Deepgram）。"""

import asyncio
import json
import time

PCM = r"D:\desktop\口语\sounds\dg_test_16k.pcm"
URL = "ws://127.0.0.1:8010/api/v2/stt/stream?language=en"


async def main() -> None:
    import websockets

    data = open(PCM, "rb").read()
    dur = len(data) / 32000
    t0 = time.time()
    first_partial = None
    final_at = None
    done_at = None
    print(f"音频 {dur:.2f}s，开始推流…")
    async with websockets.connect(URL, max_size=None) as ws:
        async def sender():
            chunk = 3200
            for i in range(0, len(data), chunk):
                await ws.send(data[i:i + chunk])
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "stop"}))

        async def receiver():
            nonlocal first_partial, final_at, done_at
            async for raw in ws:
                ev = json.loads(raw)
                t = time.time() - t0
                typ = ev.get("type")
                if typ == "partial" and first_partial is None:
                    first_partial = t
                    print(f"[{t:5.2f}s] partial: {ev.get('text')}")
                elif typ == "final":
                    final_at = t
                    print(f"[{t:5.2f}s] final:   {ev.get('text')}")
                elif typ == "done":
                    done_at = t
                    print(f"[{t:5.2f}s] done:    {ev.get('text')}")
                    break
                elif typ == "error":
                    print("ERROR:", ev)
                    break

        await asyncio.gather(sender(), receiver())
    print(f"\n首 partial: {first_partial and round(first_partial,2)}s | final: {final_at and round(final_at,2)}s | done: {done_at and round(done_at,2)}s")


asyncio.run(main())
