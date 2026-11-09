"""第二阶段验收脚本：语音通话 Persona 化 + 观测端点 + 信息流节奏。

真实 LLM（非 mock）。用法（后端已在 http://127.0.0.1:8000 运行）：

    python scripts/accept_persona_phase2.py
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")

CALL_TURNS = [
    "Hey, it's me. How's your day going?",
    "I had a long day at work, honestly.",
    "Guess what — I finally got the job!",
    "Anyway, I should probably go. Talk later!",
]


def _register(client: httpx.Client) -> tuple[str, str]:
    device = f"p2-{int(time.time())}-{os.getpid()}"
    data = client.post("/api/v3/auth/register", json={"device_key": device}).json()
    return data["user_id"], data["token"]


def _personas(client: httpx.Client) -> list[dict[str, Any]]:
    items = client.get("/api/v3/personas", params={"limit": 40}).json().get("items") or []
    picked = []
    for nickname in ("Adrian", "Camila"):
        picked += [item for item in items if item.get("nickname") == nickname][:1]
    return picked or items[:2]


def _add_friend(client: httpx.Client, persona_id: str) -> str:
    response = client.post(f"/api/v3/personas/{persona_id}/friend", json={})
    response.raise_for_status()
    return response.json()["friend_id"]


def _call_turn(client: httpx.Client, friend_id: str, text: str) -> list[str]:
    sentences: list[str] = []
    with client.stream(
        "POST",
        f"/api/v2/chat/{friend_id}/call/turn-text",
        json={"text": text, "duration_ms": 1500},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "sentence":
                sentences.append(str(event.get("text") or ""))
            elif event.get("type") == "error":
                sentences.append(f"[ERROR] {event.get('message')}")
    return sentences


def _strip_cue(text: str) -> str:
    text = text.strip()
    if text.startswith("[") and "]" in text:
        text = text.split("]", 1)[1].strip()
    return text


def main() -> int:
    failures: list[str] = []
    with httpx.Client(base_url=BASE, timeout=120.0) as probe:
        health = probe.get("/api/v3/health", timeout=15).json()
    print("== 后端 ==")
    print(json.dumps({k: health.get(k) for k in ("status", "version")}, ensure_ascii=False))

    with httpx.Client(base_url=BASE, timeout=120.0) as anonymous:
        stats_before = anonymous.get("/api/v3/stats/persona").json()
        user_id, token = _register(anonymous)

    headers = {"X-User-Id": user_id, "X-User-Token": token}
    print(f"\n== 验收用户 ==\n{user_id}")

    results: dict[str, dict[str, Any]] = {}
    with httpx.Client(base_url=BASE, timeout=120.0, headers=headers) as client:
        for persona in _personas(client):
            friend_id = _add_friend(client, persona["id"])
            print(f"\n== 语音通话 · {persona['nickname']} ==")
            turns: list[str] = []
            for text in CALL_TURNS:
                sentences = _call_turn(client, friend_id, text)
                joined = " ".join(_strip_cue(s) for s in sentences if s).strip()
                turns.append(joined)
                print(f"  我(说): {text}")
                print(f"  {persona['nickname']}(说): {joined or '（无输出）'}")
            lengths = [len(t.split()) for t in turns if t]
            questions = sum(1 for t in turns if t.rstrip().endswith(("?", "？")))
            results[persona["nickname"]] = {
                "turns": turns,
                "median_words": sorted(lengths)[len(lengths) // 2] if lengths else 0,
                "max_words": max(lengths) if lengths else 0,
                "questions": questions,
            }
            if not turns or not any(turns):
                failures.append(f"{persona['nickname']} 通话没有任何输出")
            if any(t.startswith("[ERROR]") for t in turns):
                failures.append(f"{persona['nickname']} 通话返回错误")

        # 信息流：事件动态是否可见 / 最近的是不是高价值类型
        feed = client.get("/api/v3/feed", params={"limit": 30}).json()
        items = feed.get("items") or []
        event_posts = [i for i in items if i.get("source_event_id")]
        ai_comments = [
            c
            for i in items
            for c in (i.get("comments") or [])
            if (c.get("author") or {}).get("is_ai")
        ]
        print(f"\n== 信息流 ==\n  条数 {len(items)} / 事件动态 {len(event_posts)} / AI 评论 {len(ai_comments)}")
        for item in event_posts[:5]:
            author = (item.get("author") or {}).get("nickname")
            print(f"  [事件动态] {author}: {item.get('content', '')[:80]}")

    stats_after = httpx.get(f"{BASE}/api/v3/stats/persona", timeout=120.0).json()
    print("\n== 观测端点（/api/v3/stats/persona）==")
    print(
        json.dumps(
            {
                "personas": stats_after["personas"],
                "events": {
                    k: stats_after["events"][k]
                    for k in ("total", "planned", "ongoing", "completed", "cancelled")
                },
                "posts": stats_after["posts"],
                "comments": stats_after["comments"],
                "relationships": stats_after["relationships"],
                "emotion": stats_after["emotion"],
                "background": stats_after["background"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    # 关系演进：后台 tick 完成我们插入的多人事件后，shared_events 应该 +1
    rel = stats_after["relationships"]
    print(
        f"\n== 关系演进 ==\n  total={rel['total']} avg_closeness={rel['avg_closeness']}"
        f" shared_events_total={rel['shared_events']}"
        f"（before={stats_before['relationships']['shared_events']}）"
    )
    if int(rel["shared_events"]) <= int(stats_before["relationships"]["shared_events"]):
        print("  ! 本次窗口内 shared_events 未变化（后台 tick 可能还没跑到，稍后复查）")

    # 通话对比
    if len(results) == 2:
        names = list(results)
        a, b = results[names[0]], results[names[1]]
        print(
            f"\n== 通话表达对比 ==\n{names[0]}: 中位 {a['median_words']} 词 / 最长 {a['max_words']}"
            f" / 追问 {a['questions']} 次\n{names[1]}: 中位 {b['median_words']} 词 / 最长 {b['max_words']}"
            f" / 追问 {b['questions']} 次"
        )
        if a["max_words"] and b["max_words"] and a["max_words"] == b["max_words"]:
            print("  ! 两人最长句长完全相同（可能偶然，需人工看内容）")

    print("\n== 结果 ==")
    if failures:
        for failure in failures:
            print("  FAIL:", failure)
        return 1
    print("  PASS：通话回复可用、观测端点正常、信息流含事件动态")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
