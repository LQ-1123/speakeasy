"""端到端冒烟测试：不需要麦克风，不需要真实 LLM key。

用法::

    # 1) 先启动后端：python -m uvicorn app.main:app --app-dir backend --port 8000
    # 2) 另开终端跑：
    python scripts/e2e_test.py
    python scripts/e2e_test.py --persona raj --mode task
    python scripts/e2e_test.py --synthesize-audio     # 额外测 TTS 音频帧
    python scripts/e2e_test.py --url ws://127.0.0.1:8000/ws

它会：连 WS → hello → 发文字 → 收集 assistant_delta / assistant_sentence / audio / metrics
→ 打印延迟分解表 + 音频帧统计 → 判定是否满足 SLO（首音 < 1000ms）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from dataclasses import dataclass, field

try:
    import websockets
except ImportError:
    print("需要 websockets: pip install websockets")
    sys.exit(1)


class C:
    OK = "\033[92m" if sys.stdout.isatty() else ""
    WARN = "\033[93m" if sys.stdout.isatty() else ""
    ERR = "\033[91m" if sys.stdout.isatty() else ""
    DIM = "\033[2m" if sys.stdout.isatty() else ""
    B = "\033[1m" if sys.stdout.isatty() else ""
    X = "\033[0m" if sys.stdout.isatty() else ""


@dataclass
class TurnResult:
    user_text: str = ""
    deltas: list[str] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    audio_frames: int = 0
    audio_bytes: int = 0
    audio_sample_rates: set[int] = field(default_factory=set)
    first_audio_at: float | None = None
    turn_start: float = 0.0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    metrics: dict = field(default_factory=dict)
    suggestions: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    text: str = ""


async def run_turn(ws, text: str, timeout: float = 30.0) -> TurnResult:
    """发一条文本并收集**属于本轮**的全部消息。

    服务端的消息用 ``turn_id`` 标识回合；必须先由 ``user_text`` 拿到本轮 id，
    再按 id 过滤后续消息，否则上一轮的尾巴（如迟到的 ``assistant_done``）
    会污染本轮统计。
    """
    r = TurnResult()
    r.turn_start = time.perf_counter()
    await ws.send(json.dumps({"type": "text", "text": text}))

    deadline = time.perf_counter() + timeout
    tid: int | None = None

    while time.perf_counter() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.perf_counter()))
        except asyncio.TimeoutError:
            break
        if isinstance(raw, (bytes, bytearray)):
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = msg.get("type")
        mtid = msg.get("turn_id")

        # 先确定本轮 turn_id
        if tid is None:
            if t == "user_text" and msg.get("text") == text:
                tid = mtid
                r.user_text = msg.get("text", "")
            elif t == "user_text" and msg.get("text", "").strip():
                # 可能是上一轮的残留回显，忽略
                continue
            else:
                # error 可能没有 turn_id，仍然记录
                if t == "error":
                    r.errors.append(msg)
                continue

        # 已知 tid，过滤掉非本轮消息
        if mtid is not None and mtid != tid:
            continue

        if t == "assistant_delta":
            r.deltas.append(msg.get("text", ""))
        elif t == "assistant_sentence":
            r.sentences.append(msg.get("text", ""))
        elif t == "audio":
            if r.first_audio_at is None:
                r.first_audio_at = time.perf_counter()
            r.audio_frames += 1
            r.audio_sample_rates.add(msg.get("sample_rate", 0))
            data = msg.get("data") or ""
            try:
                r.audio_bytes += len(base64.b64decode(data))
            except Exception:
                pass
        elif t == "turn_audio_end":
            r.audio_bytes = int(msg.get("total_bytes") or r.audio_bytes)
            r.audio_frames = max(r.audio_frames, 1)
        elif t == "assistant_done":
            r.metrics = msg.get("metrics", {}) or {}
            r.text = msg.get("text", "")
            r.done.set()
            break
        elif t == "suggestion":
            r.suggestions.extend(msg.get("items", []) or [])
        elif t == "error":
            r.errors.append(msg)
            if msg.get("fatal"):
                break

    return r


def fmt_ms(v) -> str:
    if v is None:
        return f"{C.DIM}—{C.X}"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return f"{C.DIM}—{C.X}"
    color = C.OK if f < 700 else (C.WARN if f < 1500 else C.ERR)
    return f"{color}{f:7.1f}{C.X}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="SpeakEasy 端到端冒烟测试")
    ap.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    ap.add_argument("--persona", default="oliver")
    ap.add_argument("--mode", default="free")
    ap.add_argument("--turns", type=int, default=2, help="连续跑几轮")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    print(f"\n{C.B}SpeakEasy 端到端测试{C.X}")
    print(f"{C.DIM}目标: {args.url}  角色: {args.persona}  模式: {args.mode}{C.X}\n")

    try:
        ws = await asyncio.wait_for(websockets.connect(args.url, max_size=64 * 1024 * 1024), timeout=10)
    except Exception as e:
        print(f"{C.ERR}✗ 无法连接 {args.url}{C.X}")
        print(f"  {type(e).__name__}: {e}")
        print(f"\n{C.WARN}后端启动了吗？{C.X}")
        print(f"  cd {sys.path[0] or '.'}")
        print(f"  python -m uvicorn app.main:app --app-dir backend --port 8000")
        return 1

    try:
        # ---- 1. 握手 ----
        await ws.send(json.dumps({
            "type": "hello",
            "persona_id": args.persona,
            "input_sample_rate": 16000,
            "mode": args.mode,
            "client": "e2e",
            "protocol_version": 1,
        }))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
        except asyncio.TimeoutError:
            print(f"{C.ERR}✗ 握手超时（没收到 ready）{C.X}")
            return 1

        ready = json.loads(raw)
        if ready.get("type") != "ready":
            print(f"{C.ERR}✗ 期望 ready，收到: {ready.get('type')}{C.X}")
            print(f"  {json.dumps(ready, ensure_ascii=False)[:600]}")
            return 1

        eng = ready.get("engines", {})
        llm = eng.get("llm", {}) if isinstance(eng, dict) else {}
        audio_cfg = ready.get("audio", {})

        print(f"{C.OK}✓{C.X} 握手成功  session={str(ready.get('session_id'))[:8]}…")
        print(f"  STT   : {eng.get('stt', '?')}")
        print(f"  TTS   : {eng.get('tts', '?')}")
        print(f"  LLM   : {llm.get('model', '?')}  configured={llm.get('configured')}")
        print(f"  下行   : {audio_cfg.get('sample_rate')} Hz  {audio_cfg.get('format')}")
        n_personas = len(ready.get("personas") or [])
        n_modes = len(ready.get("modes") or [])
        print(f"  角色 {n_personas} 个 / 模式 {n_modes} 个")

        if not llm.get("configured"):
            print(f"  {C.WARN}! LLM 未配置 → 使用内置规则引擎回复（链路真实，内容演示）{C.X}")
        print()

        # ---- 2. 逐轮对话 ----
        test_inputs = [
            "Hi, could I get a flat white please?",
            "I very like this coffee, it is too much good.",
            "What do you recommend for a quick lunch?",
            "Actually I need to go now, thanks a lot.",
        ]

        results: list[TurnResult] = []
        for i in range(args.turns):
            text = test_inputs[i % len(test_inputs)]
            print(f"{C.B}── 第 {i+1} 轮 ──{C.X}")
            print(f"{C.DIM}你: {text}{C.X}")

            r = await run_turn(ws, text, timeout=args.timeout)
            results.append(r)

            if r.errors:
                for e in r.errors:
                    print(f"  {C.ERR}✗ error: {e.get('code')} — {e.get('message', '')[:120]}{C.X}")

            if r.user_text:
                print(f"{C.DIM}STT 回显: {r.user_text}{C.X}")
            if r.sentences:
                print(f"{C.DIM}断句 ({len(r.sentences)}):{C.X}")
                for idx, s in enumerate(r.sentences):
                    print(f"  {C.DIM}[{idx}] {s}{C.X}")
            reply = "".join(r.deltas) or r.text
            if reply:
                print(f"{C.OK}回复:{C.X} {reply[:400]}{'' if len(reply) <= 400 else '…'}")

            if r.audio_frames:
                print(f"{C.DIM}音频: {r.audio_frames} 帧 / {r.audio_bytes} 字节 / "
                      f"{sorted(r.audio_sample_rates)} Hz{C.X}")
            else:
                print(f"{C.WARN}⚠ 没有收到音频帧（TTS 可能未启用或失败）{C.X}")

            if args.verbose and r.suggestions:
                print(f"{C.DIM}建议:{json.dumps(r.suggestions, ensure_ascii=False, indent=2)[:800]}{C.X}")

            print()

        # ---- 3. 汇总 ----
        print(f"{C.B}{'=' * 66}{C.X}")
        print(f"{C.B}延迟分解 (ms){C.X}")
        print(f"{C.B}{'=' * 66}{C.X}")
        header = f"{'轮次':<6}{'STT':>9}{'LLM首字':>11}{'首句TTS':>11}{'首音':>10}{'总时长':>10}{'句数':>7}"
        print(f"{C.DIM}{header}{C.X}")
        print(f"{C.DIM}{'-' * 66}{C.X}")

        for i, r in enumerate(results):
            print(
                f"{i+1:<6}"
                f"{fmt_ms(r.metrics.get('stt_ms')):>9}"
                f"{fmt_ms(r.metrics.get('llm_ttft_ms')):>11}"
                f"{fmt_ms(r.metrics.get('tts_first_sentence_ms')):>11}"
                f"{fmt_ms(r.metrics.get('first_audio_ms')):>10}"
                f"{fmt_ms(r.metrics.get('total_ms')):>10}"
                f"{len(r.sentences):>7}"
            )

        # SLO 判定
        print()
        firsts = [r.metrics.get("first_audio_ms") for r in results if r.metrics.get("first_audio_ms")]
        if firsts:
            avg = sum(float(x) for x in firsts) / len(firsts)
            best = min(float(x) for x in firsts)
            worst = max(float(x) for x in firsts)
            print(f"{C.B}首音延迟{C.X}: 平均 {avg:.0f}ms / 最好 {best:.0f}ms / 最差 {worst:.0f}ms")
            if worst < 1000:
                print(f"{C.OK}✓ SLO 达成（< 1000ms）{C.X}")
            elif avg < 1000:
                print(f"{C.WARN}~ 平均达标，但有抖动（最差 {worst:.0f}ms）{C.X}")
            else:
                print(f"{C.ERR}✗ 未达标（> 1000ms）。参 docs/SETUP_GPU.md §10 调优{C.X}")
        else:
            print(f"{C.WARN}没有首音延迟数据（可能没合成音频）{C.X}")

        total_audio = sum(r.audio_bytes for r in results)
        total_frames = sum(r.audio_frames for r in results)
        print(f"\n{C.B}音频{C.X}: {total_frames} 帧 / {total_audio:,} 字节 "
              f"(≈ {total_audio/2/24000:.1f}s @24kHz)")

        all_errs = [e for r in results for e in r.errors]
        if all_errs:
            print(f"\n{C.ERR}错误 {len(all_errs)} 条:{C.X}")
            for e in all_errs[:10]:
                print(f"  · {e.get('code')}: {e.get('message', '')[:140]}")
            return 1

        n_reply = sum(1 for r in results if (r.deltas or r.text))
        if n_reply == 0:
            print(f"\n{C.ERR}✗ 所有轮次都没有回复文本 —— 链路有问题{C.X}")
            return 1

        print(f"\n{C.OK}✓ 端到端链路正常（{n_reply}/{len(results)} 轮有回复）{C.X}\n")
        return 0

    finally:
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(130)
