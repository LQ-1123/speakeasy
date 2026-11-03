"""参考音频录制工具：为每个口音角色准备 5-10 秒的 zero-shot 克隆样本。

用法::

    python scripts/record_reference.py --persona oliver
    python scripts/record_reference.py --persona raj --seconds 8
    python scripts/record_reference.py --list
    python scripts/record_reference.py --validate

流程：显示要朗读的句子 → 按回车开始 → 录 N 秒 → 自动裁静音 → 归一化 → 存 16k 单声道 wav
→ 校验时长/电平静音 → 更新 manifest.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "assets" / "ref_audio"
MANIFEST = REF_DIR / "manifest.json"

# 每个角色的录制稿：必须与 .wav 内容逐字一致（克隆的硬要求）
RECORDING_SCRIPTS: dict[str, dict] = {
    "oliver": {
        "file": "oliver_rp.wav",
        "accent": "British RP",
        "label": "🇬🇧 伦敦 · Oliver（标准英音）",
        "script": "Right then, that's a flat white for you, and a pastry on the side. Cheers, guv, that's spot on.",
        "tips": [
            "非卷舌：car / park 的 r 不要卷，元音拉长",
            "t 要发出来：better → 'beh-tuh'，water → 'waw-tuh'",
            "/ɑː/ 和 /ɒ/ 分明：bath vs. lot",
            "语调克制，句尾微微上扬带点调侃",
        ],
    },
    "raj": {
        "file": "raj_indian.wav",
        "accent": "Indian English",
        "label": "🇮🇳 班加罗尔 · Raj（印度英语）",
        "script": "Actually, one small thing only — can we do it by this Friday itself? Then we can prepone the release, yes.",
        "tips": [
            "音节计时：每个音节等长，节奏均匀像打拍子",
            "语速偏快，句尾常有 'no?' / 'yes?' / 'na?'",
            "/θ/ 常读成 /t/ 或 /d/：think → 'tink'",
            "重音位置与英音不同：deVElop → 'DEvelop'",
        ],
    },
    "marco": {
        "file": "marco_italian.wav",
        "accent": "Italian English",
        "label": "🇮🇹 罗马 · Marco（意大利英语）",
        "script": "Ah, listen — the colour, it is too much, no? But the shape, the shape is beautiful. Dai, let's try again.",
        "tips": [
            "每个词都收在元音上，词尾不要吞音",
            "节奏有音乐性，重音明显、略慢",
            "插入 'allora' 'dai' 'boh' 等语气词",
            "r 用舌尖轻弹（意大利式）",
        ],
    },
    "emily": {
        "file": "emily_us.wav",
        "accent": "American English",
        "label": "🇺🇸 纽约 · Emily（通用美音）",
        "script": "Okay, so tell me a little about yourself, and why this role, why now.",
        "tips": [
            "卷舌 r 明显：car / hard 都要卷",
            "flap t：water → 'wadder'，better → 'bedder'",
            "/æ/ 更开：cat 接近 'kyat'（不要）但比英音开",
            "句尾常下沉表示确定",
        ],
    },
    "priya": {
        "file": "priya_sg.wav",
        "accent": "Singapore English",
        "label": "🇸🇬 新加坡 · Priya（新加坡英语）",
        "script": "Can, can. But ah, this one must confirm with the regional team first, okay?",
        "tips": [
            "语速快、语调相对平，起伏小",
            "句尾用 lah / lor / ah / okay 做语气标记",
            "can / cannot 表肯定否定（不是'能不能'）",
            "辅音清晰但不过度送气",
        ],
    },
}


class C:
    OK = "\033[92m" if sys.stdout.isatty() else ""
    WARN = "\033[93m" if sys.stdout.isatty() else ""
    ERR = "\033[91m" if sys.stdout.isatty() else ""
    DIM = "\033[2m" if sys.stdout.isatty() else ""
    B = "\033[1m" if sys.stdout.isatty() else ""
    X = "\033[0m" if sys.stdout.isatty() else ""


def load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"version": 1, "voices": {}}


def save_manifest(m: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record(persona: str, seconds: float) -> Path | None:
    """录音并做后处理，返回 wav 路径。"""
    spec = RECORDING_SCRIPTS[persona]

    print(f"\n{C.B}{spec['label']}{C.X}")
    print(f"{C.DIM}口音: {spec['accent']}{C.X}")
    print(f"\n{C.B}请用这个口音朗读下面这句（{seconds:.0f} 秒）：{C.X}\n")
    print(f"  {C.OK}\"{spec['script']}\"{C.X}\n")
    print(f"{C.DIM}发音要点：{C.X}")
    for tip in spec["tips"]:
        print(f"  {C.DIM}· {tip}{C.X}")

    try:
        import numpy as np
    except ImportError:
        print(f"{C.ERR}numpy 未安装{C.X} → pip install numpy")
        return None

    try:
        import sounddevice as sd
    except ImportError:
        print(f"\n{C.ERR}需要 sounddevice 录音{C.X}")
        print(f"  安装: pip install sounddevice")
        print(f"  或用任意工具录好后手动放到: {REF_DIR / spec['file']}")
        print(f"  {C.DIM}要求: wav / 单声道 / 16-bit / 任意采样率（16k 或 24k 最好）/ 5-10 秒{C.X}")
        return None

    sr = 24000  # 录高点再降采样，质量更好
    input(f"\n{C.B}按回车开始录音…{C.X} ")
    print(f"{C.ERR}● 录音中 {seconds:.0f}s …{C.X}")
    try:
        audio = sd.rec(int(sr * seconds), samplerate=sr, channels=1, dtype="float32")
        for i in range(int(seconds * 10), 0, -1):
            print(f"\r  剩余 {i/10:4.1f}s ", end="", flush=True)
            time.sleep(0.1)
        sd.wait()
        print("\r" + " " * 20 + "\r", end="")
    except Exception as e:
        print(f"{C.ERR}录音失败: {type(e).__name__}: {e}{C.X}")
        return None

    x = audio[:, 0].astype(np.float32)

    # ---- 后处理 ----
    # 1. 裁静音（首尾能量阈值）
    thresh = max(0.008, float(np.abs(x).max()) * 0.06)
    nz = np.where(np.abs(x) > thresh)[0]
    if len(nz) == 0:
        print(f"{C.ERR}没检测到有效语音（全静音）{C.X}")
        return None
    pad = int(0.15 * sr)
    a = max(0, nz[0] - pad)
    b = min(len(x), nz[-1] + pad)
    x = x[a:b]

    # 2. 峰值归一化到 -3dBFS
    peak = float(np.abs(x).max())
    if peak > 1e-6:
        x = x * (0.707 / peak)

    # 3. 降采样到 16k（线性插值，足够）
    target_sr = 16000
    if sr != target_sr:
        n_out = int(len(x) * target_sr / sr)
        idx = np.linspace(0, len(x) - 1, n_out)
        x = np.interp(idx, np.arange(len(x)), x).astype(np.float32)

    dur = len(x) / target_sr
    if dur < 3.5:
        print(f"{C.WARN}警告：只有 {dur:.1f}s，建议 5-10s（太短克隆不稳）。可重录或接受。{C.X}")
    if dur > 15:
        print(f"{C.WARN}警告：{dur:.1f}s 偏长，建议裁到 10s 内。{C.X}")

    # 4. 写 16-bit PCM wav
    out = REF_DIR / spec["file"]
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(x * 32767, -32768, 32767).astype(np.int16)
    import wave
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(target_sr)
        w.writeframes(pcm.tobytes())

    print(f"{C.OK}✓{C.X} 已保存 {out}  ({dur:.2f}s, 16kHz mono)")

    # 5. 更新 manifest
    m = load_manifest()
    m.setdefault("voices", {})[persona] = {
        "file": spec["file"],
        "ref_text": spec["script"],
        "accent": spec["accent"],
        "duration_s": round(dur, 2),
        "sample_rate": target_sr,
        "source": "self-recorded",
        "speed": 1.0,
        "notes": f"自录 {spec['accent']} 参考音频",
    }
    save_manifest(m)
    print(f"{C.OK}✓{C.X} manifest 已更新")

    return out


def validate() -> int:
    print(f"\n{C.B}=== 参考音频体检 ==={C.X}\n")
    if not REF_DIR.exists():
        print(f"{C.ERR}目录不存在: {REF_DIR}{C.X}")
        return 1

    m = load_manifest()
    voices = m.get("voices", {})
    problems = 0

    try:
        import numpy as np
        import wave
    except ImportError:
        print(f"{C.ERR}numpy 未安装{C.X}")
        return 1

    for pid, spec in RECORDING_SCRIPTS.items():
        entry = voices.get(pid)
        fname = (entry or {}).get("file") or spec["file"]
        path = REF_DIR / fname

        if not path.exists():
            print(f"{C.WARN}○{C.X} {pid:<8} 缺失 → {fname}")
            print(f"   {C.DIM}python scripts/record_reference.py --persona {pid}{C.X}")
            problems += 1
            continue

        try:
            with wave.open(str(path), "rb") as w:
                sr = w.getframerate()
                ch = w.getnchannels()
                n = w.getnframes()
                raw = w.readframes(n)
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            dur = n / sr
            peak = float(np.abs(x).max())
            rms = float(np.sqrt(np.mean(x ** 2)))

            issues = []
            if not (4.0 <= dur <= 12.0):
                issues.append(f"时长 {dur:.1f}s 不在 4-12s")
            if ch != 1:
                issues.append(f"{ch} 声道（需单声道）")
            if sr not in (16000, 24000):
                issues.append(f"采样率 {sr}（需 16k 或 24k）")
            if peak < 0.05:
                issues.append(f"音量过低 peak={peak:.3f}")
            if rms < 0.005:
                issues.append("疑似静音")
            if not (entry or {}).get("ref_text"):
                issues.append("manifest 缺 ref_text")

            if issues:
                print(f"{C.WARN}△{C.X} {pid:<8} {dur:5.2f}s {sr}Hz {ch}ch peak={peak:.2f}")
                for i in issues:
                    print(f"   {C.WARN}· {i}{C.X}")
                problems += 1
            else:
                print(f"{C.OK}✓{C.X} {pid:<8} {dur:5.2f}s {sr}Hz {ch}ch peak={peak:.2f} rms={rms:.3f}")
        except Exception as e:
            print(f"{C.ERR}✗{C.X} {pid}: {type(e).__name__}: {e}")
            problems += 1

    print()
    if problems:
        print(f"{C.WARN}{problems} 个角色需要处理。缺失时 TTS 会自动降级为 tone 后端（链路仍可跑）。{C.X}")
    else:
        print(f"{C.OK}全部就绪，可以启用 zero-shot 口音克隆。{C.X}")
    print()
    return 0 if problems == 0 else 1


def list_personas() -> None:
    print(f"\n{C.B}可录制角色{C.X}\n")
    m = load_manifest()
    for pid, spec in RECORDING_SCRIPTS.items():
        path = REF_DIR / spec["file"]
        mark = f"{C.OK}✓{C.X}" if path.exists() else f"{C.WARN}○{C.X}"
        print(f"  {mark} {pid:<8} {spec['label']}")
        print(f"      {C.DIM}{spec['script']}{C.X}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="录制零样本克隆的参考音频")
    ap.add_argument("--persona", "-p", help="角色 id（oliver/raj/marco/emily/priya）")
    ap.add_argument("--seconds", "-s", type=float, default=8.0, help="录制时长（默认 8s）")
    ap.add_argument("--list", action="store_true", help="列出所有角色")
    ap.add_argument("--validate", action="store_true", help="体检现有参考音频")
    ap.add_argument("--all", action="store_true", help="依次录制全部角色")
    args = ap.parse_args()

    if args.list:
        list_personas()
        return 0
    if args.validate:
        return validate()

    if args.all:
        for pid in RECORDING_SCRIPTS:
            if (REF_DIR / RECORDING_SCRIPTS[pid]["file"]).exists():
                print(f"{C.DIM}跳过 {pid}（已存在）{C.X}")
                continue
            record(pid, args.seconds)
        return validate()

    if not args.persona:
        list_personas()
        print(f"{C.DIM}用法: python scripts/record_reference.py --persona oliver{C.X}\n")
        return 1

    pid = args.persona.lower()
    if pid not in RECORDING_SCRIPTS:
        print(f"{C.ERR}未知角色: {pid}{C.X}（可用: {', '.join(RECORDING_SCRIPTS)}）")
        return 1

    if record(pid, args.seconds):
        return validate()
    return 1


if __name__ == "__main__":
    sys.exit(main())
