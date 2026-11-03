"""测量 TTS/STT 合成时的 GPU 显存占用。"""

import subprocess
import threading
import time

import httpx

BASE = "http://127.0.0.1:8000"


def vram():
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
        text=True,
    ).strip()
    used, util = [x.strip() for x in out.split(",")]
    return int(used), int(util)


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples = []
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                self.samples.append(vram())
            except Exception:
                pass
            time.sleep(0.08)


def report(label, samples):
    if not samples:
        print(f"  {label}: 无采样", flush=True)
        return None
    mems = [m for m, _ in samples]
    utils = [u for _, u in samples]
    print(
        f"  {label:<22} 显存 峰值 {max(mems):>6} MiB  均值 {sum(mems)//len(mems):>6} MiB"
        f"   |  GPU 峰值 {max(utils):>3}%  均值 {sum(utils)//len(utils):>3}%",
        flush=True,
    )
    return max(mems)


def main():
    c = httpx.Client(base_url=BASE, timeout=180)

    total = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], text=True
    ).strip()
    print(f"  显卡总显存: {total} MiB", flush=True)

    # ---- 1) 空闲基线 ----
    s = Sampler()
    s.start()
    time.sleep(3)
    s.stop_flag = True
    s.join()
    idle_peak = report("空闲（模型已加载）", s.samples)

    # ---- 2) 取可用声线 ----
    accents = c.get("/api/market/accents").json()
    rows = accents.get("accents", accents.get("items", []))
    voice = rows[0]["voice"] if rows and "voice" in rows[0] else "en-GB-RyanNeural"
    print(f"\n  测试声线: {voice}", flush=True)

    # ---- 3) 连续合成，采样 ----
    s2 = Sampler()
    s2.start()
    t0 = time.perf_counter()
    ok = 0
    for i in range(6):
        try:
            r = c.post("/api/market/preview", json={"voice": voice, "text": f"Testing number {i}. Have a nice day."})
            if r.status_code == 200:
                ok += 1
        except Exception as e:
            print("   合成失败:", e, flush=True)
    elapsed = time.perf_counter() - t0
    time.sleep(0.4)
    s2.stop_flag = True
    s2.join()

    print(f"\n  连续合成 {ok}/6 次，耗时 {elapsed:.1f}s（平均 {elapsed/max(ok,1):.2f}s/次）", flush=True)
    tts_peak = report("TTS 合成中", s2.samples)

    # ---- 4) 完整一轮对话（STT + LLM + TTS）----
    import asyncio
    import struct

    import websockets

    async def turn():
        async with websockets.connect("ws://127.0.0.1:8000/ws", max_size=16 * 1024 * 1024) as ws:
            await ws.send('{"type":"hello","protocol":1}')
            await asyncio.wait_for(ws.recv(), timeout=120)
            # 用 SAPI 已生成的 16k PCM 走一轮语音
            pcm = open(r"D:\desktop\口语\data\ptt\speech16k.raw", "rb").read()
            await ws.send('{"type":"ptt_hold"}')
            for i in range(0, len(pcm), 1024):
                await ws.send(pcm[i:i + 1024])
                await asyncio.sleep(0.032)
            await ws.send('{"type":"audio_end"}')
            end = asyncio.get_event_loop().time() + 40
            while asyncio.get_event_loop().time() < end:
                try:
                    r = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not isinstance(r, bytes) and '"assistant_done"' in r:
                    return True
            return False

    import os
    if os.path.exists(r"D:\desktop\口语\data\ptt\speech16k.raw"):
        s3 = Sampler()
        s3.start()
        done = asyncio.run(turn())
        time.sleep(0.5)
        s3.stop_flag = True
        s3.join()
        print(f"\n  完整一轮（语音输入→STT→LLM→TTS）完成: {done}", flush=True)
        full_peak = report("整轮对话中", s3.samples)
    else:
        full_peak = None
        print("\n  （缺测试音频，跳过整轮测试）", flush=True)

    print("\n=== 参考 ===", flush=True)
    print(f"  总显存 {total} MiB", flush=True)
    print(f"  空闲占用 {idle_peak} MiB", flush=True)
    print(f"  TTS 峰值 {tts_peak} MiB", flush=True)
    if full_peak:
        print(f"  整轮峰值 {full_peak} MiB", flush=True)
    peak = max([x for x in (idle_peak, tts_peak, full_peak) if x])
    print(f"  >>> 最高占用 {peak} MiB，剩余 {int(total) - peak} MiB", flush=True)
    c.close()


main()
