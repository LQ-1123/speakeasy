import math, wave, struct, os

SR = 44100

def bell(path, notes, gain=0.9):
    """notes: [(freq, start_s, dur_s, amp)] —— 带泛音和指数衰减的铃声"""
    total = 0.0
    for _, st, dur, _a in notes:
        total = max(total, st + dur)
    n = int(total * SR) + 1
    buf = [0.0] * n
    for freq, st, dur, amp in notes:
        s0 = int(st * SR)
        for i in range(int(dur * SR)):
            t = i / SR
            env = math.exp(-t * 5.2)
            # 基频 + 两个泛音，模拟金属/钢琴质感
            v = (
                math.sin(2 * math.pi * freq * t) * 1.0
                + math.sin(2 * math.pi * freq * 2.01 * t) * 0.35
                + math.sin(2 * math.pi * freq * 3.02 * t) * 0.16
            ) * env * amp
            idx = s0 + i
            if idx < n:
                buf[idx] += v
    peak = max(1e-6, max(abs(x) for x in buf))
    scale = gain / peak
    frames = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x * scale)) * 32767)) for x in buf)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(frames)
    print(path, os.path.getsize(path), "bytes")

os.makedirs("sounds", exist_ok=True)
# 新消息：类似微信「叮」——E6 单音 + 高八度弱泛音
bell("sounds/notify_message.wav", [(1318.5, 0.00, 0.55, 1.0), (2637.0, 0.00, 0.30, 0.25)])
# 新好友：两声上行「叮咚」——A5 → E6
bell("sounds/notify_request.wav", [(880.0, 0.00, 0.45, 1.0), (1318.5, 0.22, 0.55, 0.95)])
