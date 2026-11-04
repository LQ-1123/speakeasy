"""全链路冒烟 · API 层。逐环节记录状态、耗时、关键字段。"""

import json
import time

import httpx

B = "http://127.0.0.1:8000/api/v2"
rows: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, detail: str) -> None:
    rows.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def get(path, **kw):
    return httpx.get(B + path, timeout=kw.pop("timeout", 60), **kw)


# ---------------------------------------------------------------- 0) 健康
try:
    h = get("/health", timeout=30)
    step("健康检查", h.status_code == 200, f"{h.status_code} {h.json().get('status', '')}".strip())
except Exception as exc:  # noqa: BLE001
    step("健康检查", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 1) 设置读取
try:
    s = get("/settings", timeout=30).json()
    llm = s["llm"]["config"]
    stt = s["stt"]["config"]
    step(
        "设置读取",
        bool(llm.get("model")) and bool(stt.get("provider")),
        f"llm={llm['model']} key={llm['api_key_set']} | stt={stt['provider']}",
    )
except Exception as exc:  # noqa: BLE001
    step("设置读取", False, str(exc))

# ---------------------------------------------------------------- 2) 捏人（discover）
new_friend = None
try:
    t0 = time.time()
    r = httpx.post(B + "/friends/discover", json={"language": "en"}, timeout=180)
    dt = time.time() - t0
    if r.status_code == 200:
        d = r.json()
        new_friend = d.get("friend") or {}
        step(
            "捏人 discover",
            bool(new_friend.get("id")),
            f"{dt:.1f}s → {new_friend.get('name')} / {new_friend.get('gender')} / "
            f"{new_friend.get('nationality')} / 音色 {str(new_friend.get('fish_voice_id'))[:8]}",
        )
    else:
        step("捏人 discover", False, f"{r.status_code} {r.text[:120]}")
except Exception as exc:  # noqa: BLE001
    step("捏人 discover", False, f"{type(exc).__name__}: {exc}")

fid = new_friend.get("id") if new_friend else None
if not fid:
    friends = get("/friends?limit=1", timeout=30).json()["friends"]
    fid = friends[0]["id"]
    print(f"  （回退到已有好友 {fid[:8]}）", flush=True)

# ---------------------------------------------------------------- 3) 文字聊天
try:
    t0 = time.time()
    r = httpx.post(
        B + f"/chat/{fid}/text",
        json={"text": "Hey! What are you up to today?", "context": None},
        timeout=180,
    )
    dt = time.time() - t0
    msgs = get(f"/friends/{fid}/messages?limit=6", timeout=60).json()["messages"]
    last_friend = next((m for m in reversed(msgs) if m.get("sender") == "friend"), {})
    step(
        "文字聊天",
        r.status_code == 200 and bool(last_friend.get("transcript")),
        f"{dt:.1f}s → 回复「{(last_friend.get('transcript') or '')[:48]}」"
        f" audio={'有' if last_friend.get('audio_url') else '无'}",
    )
except Exception as exc:  # noqa: BLE001
    step("文字聊天", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 4) 语音聊天
try:
    audio = open(r"D:\desktop\口语\data\audio\fish_1f9ba5645d0a.mp3", "rb").read()
    t0 = time.time()
    r = httpx.post(
        B + f"/chat/{fid}/voice",
        files={"audio": ("voice.mp3", audio, "audio/mpeg")},
        data={"duration_ms": "3800"},
        timeout=240,
    )
    dt = time.time() - t0
    msgs = get(f"/friends/{fid}/messages?limit=6", timeout=60).json()["messages"]
    user_voice = next((m for m in reversed(msgs) if m.get("sender") == "user"), {})
    friend_voice = next((m for m in reversed(msgs) if m.get("sender") == "friend"), {})
    step(
        "语音聊天",
        r.status_code == 200 and bool(user_voice.get("transcript")) and bool(friend_voice.get("audio_url")),
        f"{dt:.1f}s → 转写「{(user_voice.get('transcript') or '')[:36]}」 / "
        f"回复音频 {'有' if friend_voice.get('audio_url') else '无'}",
    )
except Exception as exc:  # noqa: BLE001
    step("语音聊天", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 5) 翻译
try:
    t0 = time.time()
    r = httpx.post(
        B + "/translate",
        json={"text": "I've been meaning to ask you about that.", "friend_id": fid},
        timeout=120,
    )
    dt = time.time() - t0
    zh = (r.json() or {}).get("translation") or (r.json() or {}).get("text_zh") or ""
    step("翻译", r.status_code == 200 and bool(zh), f"{dt:.1f}s → {zh[:40]}")
except Exception as exc:  # noqa: BLE001
    step("翻译", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 6) 朋友圈
try:
    lst = get("/moments", timeout=60).json()["moments"]
    t0 = time.time()
    r = httpx.post(B + "/moments", json={"content": "今天的晚霞是橘色的，像被谁打翻了颜料。", "mood_tag": "🌇 傍晚"}, timeout=120)
    dt = time.time() - t0
    step(
        "朋友圈读取+发布",
        r.status_code == 200,
        f"现存 {len(lst)} 条 | 发布 {dt:.1f}s → {r.status_code}",
    )
    print("  等待 AI 好友评论（后台生成）…", flush=True)
    time.sleep(18)
    lst2 = get("/moments", timeout=60).json()["moments"]
    mine = next((m for m in lst2 if m.get("author_type") == "user"), {})
    comments = mine.get("comments") or []
    step(
        "朋友圈 AI 评论",
        True,
        f"我的动态 {len(lst2)-len(lst)} 条新增，评论 {len(comments)} 条"
        + (f"：「{(comments[0].get('content') or comments[0].get('text') or '')[:36]}」" if comments else ""),
    )
except Exception as exc:  # noqa: BLE001
    step("朋友圈", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 7) 表达本
try:
    created = httpx.post(
        B + "/expressions",
        json={"original_text": "I very like it", "better_expression": "I really like it", "explanation": "really 比 very 更自然"},
        timeout=60,
    ).json()["expression"]
    eid = created["id"]
    patched = httpx.patch(B + f"/expressions/{eid}", json={"status": "mastered", "mastery_count": 3}, timeout=60)
    patched_ok = patched.status_code == 200
    lst = get("/expressions", timeout=60).json()["expressions"]
    removed = httpx.delete(B + f"/expressions/{eid}", timeout=60)
    step(
        "表达本增改删",
        patched_ok and removed.status_code == 200,
        f"新增→PATCH(mastered)→列表 {len(lst)} 条→DELETE {removed.status_code}",
    )
except Exception as exc:  # noqa: BLE001
    step("表达本", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 8) 换口音
try:
    arche = get("/archetypes", timeout=60).json()["archetypes"]
    friend = get(f"/friends/{fid}", timeout=30).json()["friend"]
    lang = friend.get("reply_language") or "en"
    same = [a for a in arche if (a.get("locale_info") or {}).get("native_lang") == lang]
    target = (same or arche)[0]
    before_voice = friend.get("voice_id")
    r = httpx.patch(B + f"/friends/{fid}/voice", json={"archetype_id": target["archetype_id"]}, timeout=60)
    after = get(f"/friends/{fid}", timeout=30).json()["friend"]
    step(
        "换口音",
        r.status_code == 200 and after.get("voice_id") != before_voice,
        f"{target.get('accent_label')} → {after.get('voice_id')} (rate {after.get('voice_rate')}, pitch {after.get('voice_pitch')})",
    )
    # 换回
    if same:
        old = next((a for a in arche if a.get("voice_id") == before_voice), None)
        if old:
            httpx.patch(B + f"/friends/{fid}/voice", json={"archetype_id": old["archetype_id"]}, timeout=60)
except Exception as exc:  # noqa: BLE001
    step("换口音", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 9) 通话
try:
    w = get(f"/chat/{fid}/call/warmup", timeout=60)
    audio = open(r"D:\desktop\口语\data\audio\fish_1f9ba5645d0a.mp3", "rb").read()
    t0 = time.time()
    events = []
    with httpx.stream(
        "POST",
        B + f"/chat/{fid}/call/turn",
        files={"audio": ("call.mp3", audio, "audio/mpeg")},
        data={"duration_ms": "3600"},
        timeout=240,
    ) as resp:
        buf = b""
        for chunk in resp.iter_bytes():
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    events.append(json.loads(line))
    sents = [e for e in events if e.get("type") == "sentence"]
    summ = httpx.post(B + f"/chat/{fid}/call/summary", json={"duration_s": 24, "turns": len(sents) or 1}, timeout=60)
    step(
        "通话流式回合",
        w.status_code == 200 and bool(sents) and summ.status_code == 200,
        f"warmup {w.status_code} | {len(events)} 事件 / {len(sents)} 句 / {time.time()-t0:.1f}s | summary {summ.status_code}",
    )
except Exception as exc:  # noqa: BLE001
    step("通话", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 70)
passed = sum(1 for r in rows if r[1] == "PASS")
print(f"API 冒烟：{passed}/{len(rows)} 通过")
for name, status, detail in rows:
    print(f"  [{status}] {name}")

out = "docs/smoke-api.json"
import os
os.makedirs("docs", exist_ok=True)
json.dump([{"step": n, "status": s, "detail": d} for n, s, d in rows], open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"\n结果写入 {out}；本次新好友 id={fid}")
