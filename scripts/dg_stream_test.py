"""Deepgram 流式 STT 基准测试：用服务器 TTS 生成的 wav 模拟实时说话。"""

import asyncio
import json
import os
os.environ["ALL_PROXY"] = "http://127.0.0.1:7890"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
os.environ.pop("NO_PROXY", None)
import time
import wave

KEY = "483e52a1a1bcbfe5634312c9e7c188c444870540"
WAV = r"D:\desktop\口语\sounds\dg_test.wav"
OUT = r"D:\desktop\口语\sounds\dg_test_16k.pcm"


def wav_to_16k_pcm(path: str, out: str) -> float:
    """服务器 TTS 返回的是流式 WAV（RIFF 长度为 0xFFFFFFFF），不能依赖 wave 模块的头。"""
    import struct

    raw = open(path, "rb").read()
    i = raw.find(b"data")
    assert i > 0, "no data chunk"
    data = raw[i + 8:]
    n = len(data) // 2
    samples = struct.unpack("<%dh" % n, data)
    sr = 44100
    target = 16000
    out_len = int(len(samples) * target / sr)
    pcm = bytearray()
    for i in range(out_len):
        pos = i * sr / target
        i0 = int(pos)
        i1 = min(i0 + 1, len(samples) - 1)
        frac = pos - i0
        v = int(samples[i0] * (1 - frac) + samples[i1] * frac)
        pcm += struct.pack("<h", max(-32768, min(32767, v)))
    open(out, "wb").write(pcm)
    return out_len / target


async def main() -> None:
    import websockets

    dur = wav_to_16k_pcm(WAV, OUT)
    data = open(OUT, "rb").read()
    url = (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-3&language=en&encoding=linear16&sample_rate=16000"
        "&channels=1&interim_results=true&smart_format=true&punctuate=true"
        "&endpointing=300&utterance_end_ms=1000"
    )
    headers = {"Authorization": f"Token {KEY}"}
    t0 = time.time()
    first_partial = None
    async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:

        async def sender():
            chunk = 3200  # 100ms @16k
            for i in range(0, len(data), chunk):
                await ws.send(data[i:i + chunk])
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "Finalize"}))
            await asyncio.sleep(0.5)
            await ws.send(json.dumps({"type": "CloseStream"}))

        async def receiver():
            nonlocal first_partial
            finals = []
            async for msg in ws:
                ev = json.loads(msg)
                t = time.time() - t0
                if ev.get("type") == "Results":
                    alt = ev["channel"]["alternatives"][0]
                    txt = alt.get("transcript", "")
                    if not txt:
                        continue
                    if not ev.get("is_final"):
                        if first_partial is None:
                            first_partial = t
                            print(f"[{t:5.2f}s] 首个中间结果: {txt}")
                    else:
                        finals.append(txt)
                        print(f"[{t:5.2f}s] 最终: {txt}")
                elif ev.get("type") == "Metadata":
                    print(f"[{t:5.2f}s] 结束, 请求 {ev.get('request_id','')[:8]}")
                    break
                elif ev.get("type") == "Error":
                    print("ERROR:", ev)
                    break
            return finals

        await asyncio.gather(sender(), receiver())
    total = time.time() - t0
    print(f"\n音频时长 {dur:.2f}s | 首中间结果 {first_partial and round(first_partial,2)}s | 总耗时 {total:.2f}s")


asyncio.run(main())
