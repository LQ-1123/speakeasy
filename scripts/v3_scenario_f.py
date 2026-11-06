"""阶段 1 场景 F：原有链路回归（文字/语音识别/语音合成/实时通话/好友/资料/记忆）。

用真实后端 + 真实 LLM/STT/TTS 跑一遍，身份用浏览器里注册的设备用户，
从而同时验证「带身份头的 v2 链路」与「V3 加好友后的聊天」。

运行：D:\\speakeasy-venv\\Scripts\\python.exe scripts\\v3_scenario_f.py
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.getenv("SE_BASE", "http://127.0.0.1:8000")


def load_identity() -> dict:
    path = os.path.join("data", "e2e-identity.json")
    raw = open(path, encoding="utf-8-sig").read().strip()
    data = json.loads(raw)
    if isinstance(data, str):
        data = json.loads(data)
    return data


def main() -> int:
    ident = load_identity()
    headers = {"X-User-Id": ident["user_id"], "X-User-Token": ident["token"]}
    client = httpx.Client(base_url=BASE, timeout=180.0)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1. 好友列表（V3 加的两个好友应在此）
    r = client.get("/api/v2/friends", headers=headers)
    friends = r.json().get("friends", []) if r.status_code == 200 else []
    names = [f.get("name") for f in friends]
    check("好友列表", r.status_code == 200 and len(friends) >= 2, f"names={names}")
    if not friends:
        return 1
    fid = friends[0]["id"]

    # 2. 好友资料 + 记忆
    r = client.get(f"/api/v2/friends/{fid}", headers=headers)
    data = r.json() if r.status_code == 200 else {}
    check(
        "好友资料/记忆",
        r.status_code == 200 and bool(data.get("friend")) and isinstance(data.get("memory"), dict),
        f"closeness={data.get('memory', {}).get('closeness_level')}",
    )
    r = client.get("/api/v2/memories", headers=headers)
    check("用户画像/记忆", r.status_code == 200 and "profile" in (r.json() if r.status_code == 200 else {}),
          f"status={r.status_code}")

    # 3. 文字聊天（真实 LLM）
    t0 = time.time()
    r = client.post(f"/api/v2/chat/{fid}/text", headers=headers,
                    json={"text": "Hi! What's the best thing that happened to you today?"})
    body = r.json() if r.status_code == 200 else {}
    reply = " ".join(m.get("transcript", "") for m in body.get("friend_messages", []))
    check("AI 文字聊天", r.status_code == 200 and bool(reply.strip()),
          f"{time.time() - t0:.1f}s reply={reply[:80]!r}")

    # 4. 语音合成（TTS）
    t0 = time.time()
    r = client.get("/api/v2/tts/sample",
                   params={"text": "This is a quick voice check for SpeakEasy.", "voice": friends[0].get("voice_id") or "en-GB-RyanNeural"},
                   headers=headers)
    audio = r.content if r.status_code == 200 else b""
    check("语音合成 TTS", r.status_code == 200 and len(audio) > 4000,
          f"{time.time() - t0:.1f}s bytes={len(audio)} type={r.headers.get('content-type')}")

    # 5. 语音识别（把刚合成的音频喂回去，走真实 STT）
    stt_detail = "skipped"
    stt_ok = False
    if len(audio) > 4000:
        t0 = time.time()
        files = {"audio": ("probe.wav" if audio[:4] == b"RIFF" else "probe.mp3", audio, "audio/wav")}
        r = client.post(f"/api/v2/chat/{fid}/voice", headers=headers, files=files,
                        data={"duration_ms": "3000"})
        body = r.json() if r.status_code == 200 else {}
        stt = body.get("stt") or {}
        transcript = (body.get("user_message") or {}).get("transcript", "")
        stt_ok = r.status_code == 200 and bool(transcript.strip())
        stt_detail = f"{time.time() - t0:.1f}s backend={stt.get('backend')} text={transcript[:70]!r}"
    check("语音识别 STT", stt_ok, stt_detail)

    # 6. 实时通话（文本回合，NDJSON 流）
    t0 = time.time()
    events: list[dict] = []
    with client.stream("POST", f"/api/v2/chat/{fid}/call/turn-text", headers=headers,
                       json={"text": "Can you hear me? Let's practice for a minute."}) as resp:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    kinds = [e.get("type") for e in events]
    check("实时通话（流式回合）", len(events) > 0 and "done" in kinds,
          f"{time.time() - t0:.1f}s events={kinds[:6]}")

    # 7. 表达本 / 资料
    r = client.get("/api/v2/expressions", headers=headers)
    check("表达本", r.status_code == 200, f"status={r.status_code}")
    r = client.get("/api/v2/bootstrap", headers=headers)
    b = r.json() if r.status_code == 200 else {}
    check("bootstrap（资料+会话）", r.status_code == 200 and "profile" in b,
          f"friends={len(b.get('friends', []))}")

    # 8. 记忆写入校验（聊天后 total_turns 应增加）
    r = client.get(f"/api/v2/friends/{fid}", headers=headers)
    mem = (r.json() or {}).get("memory", {}) if r.status_code == 200 else {}
    check("聊天后记忆累积", int(mem.get("total_turns") or 0) >= 2, f"total_turns={mem.get('total_turns')}")

    failed = [x for x in results if not x[1]]
    print(f"\n汇总: {len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
