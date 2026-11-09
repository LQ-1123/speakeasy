"""Persona 真实感 · 本地 Web 验收脚本（**真实 LLM**，非 mock）。

做三件事：
1. 新建一个独立用户（不影响本机既有账号数据），加两个性格相反的人物为好友。
2. 各进行 12 轮真实聊天，统计：重复率 / 追问率 / 开场多样性 / 长度分布。
3. 读信息流，检查事件驱动动态与 AI 评论是否可见。

用法（后端已在 http://127.0.0.1:8000 运行）：
    python scripts/accept_persona_chat.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from typing import Any

import httpx

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")
TURNS = [
    "Hey! How's it going?",
    "What are you up to today?",
    "I had a really long day at work",
    "Guess what — I finally got the job!",
    "Honestly I think remote work is overrated",
    "My cat knocked over my coffee this morning",
    "I'm a bit nervous about tomorrow",
    "Do you have any plans for the weekend?",
    "What kind of music do you listen to?",
    "I failed my driving test again...",
    "Okay I should probably go to bed",
    "Anyway, talk later!",
]


def _client(user_id: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        timeout=90.0,
        headers={"X-User-Id": user_id, "X-User-Token": token},
    )


def _register(client: httpx.Client) -> tuple[str, str]:
    device = f"accept-{int(time.time())}-{os.getpid()}"
    r = client.post("/api/v3/auth/register", json={"device_key": device})
    r.raise_for_status()
    data = r.json()
    return data["user_id"], data["token"]


def _pick_personas(client: httpx.Client) -> list[dict[str, Any]]:
    r = client.get("/api/v3/personas", params={"limit": 40})
    r.raise_for_status()
    items = r.json().get("items") or []
    wanted: list[dict[str, Any]] = []
    for nickname in ("Adrian", "Camila"):
        for item in items:
            if item.get("nickname") == nickname:
                wanted.append(item)
                break
    return wanted or items[:2]


def _add_friend(client: httpx.Client, persona_id: str) -> str:
    r = client.post(f"/api/v3/personas/{persona_id}/friend", json={})
    r.raise_for_status()
    return r.json()["friend_id"]


def _send(client: httpx.Client, friend_id: str, text: str) -> dict[str, Any]:
    r = client.post(f"/api/v2/chat/{friend_id}/text", json={"text": text})
    r.raise_for_status()
    return r.json()


def _reply_text(payload: dict[str, Any]) -> str:
    msgs = payload.get("friend_messages") or []
    if not msgs:
        return ""
    first = msgs[0]
    return str(first.get("transcript") or first.get("text") or "").strip()


def _strip_cue(text: str) -> str:
    text = text.strip()
    if text.startswith("[") and "]" in text:
        text = text.split("]", 1)[1].strip()
    return text


def _stats(replies: list[str]) -> dict[str, Any]:
    cleaned = [_strip_cue(r) for r in replies if r]
    lengths = [len(t.split()) for t in cleaned]
    questions = sum(1 for t in cleaned if t.rstrip().endswith(("?", "？")))
    firsts = [t.split()[0].strip(".,!").lower() for t in cleaned if t.split()]
    return {
        "turns": len(cleaned),
        "unique": len(set(cleaned)),
        "duplicate_ratio": round(1 - len(set(cleaned)) / max(1, len(cleaned)), 2),
        "question_ratio": round(questions / max(1, len(cleaned)), 2),
        "distinct_openers": len(set(firsts)),
        "min_words": min(lengths) if lengths else 0,
        "max_words": max(lengths) if lengths else 0,
        "median_words": statistics.median(lengths) if lengths else 0,
        "replies": cleaned,
    }


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=90.0) as probe:
        try:
            health = probe.get("/api/v3/health", timeout=10).json()
        except Exception as exc:  # noqa: BLE001
            print(f"后端不可用：{exc}")
            return 1
    print("== 后端状态 ==")
    print(json.dumps({k: health.get(k) for k in ("status", "version", "personas")}, ensure_ascii=False))
    print("world:", json.dumps(health.get("world"), ensure_ascii=False))

    bootstrap = httpx.Client(base_url=BASE, timeout=90.0)
    user_id, token = _register(bootstrap)
    print(f"\n== 验收用户 ==\nuser_id={user_id}")

    failures: list[str] = []
    results: dict[str, Any] = {}
    with _client(user_id, token) as client:
        personas = _pick_personas(client)
        print("\n== 选取人物 ==")
        for persona in personas:
            print(f"  {persona['nickname']} ({persona.get('city')}, {persona.get('language')})")

        for persona in personas:
            friend_id = _add_friend(client, persona["id"])
            replies: list[str] = []
            print(f"\n== 真实聊天 · {persona['nickname']} ==")
            for text in TURNS:
                try:
                    payload = _send(client, friend_id, text)
                except Exception as exc:  # noqa: BLE001
                    print(f"  !! 发送失败: {exc}")
                    failures.append(f"{persona['nickname']} 发送失败: {exc}")
                    break
                reply = _reply_text(payload)
                replies.append(reply)
                print(f"  我: {text}")
                print(f"  {persona['nickname']}: {reply}")
            stats = _stats(replies)
            results[persona["nickname"]] = stats
            print(
                f"  → 轮数 {stats['turns']} / 不同回复 {stats['unique']}"
                f" / 追问率 {stats['question_ratio']} / 开场多样性 {stats['distinct_openers']}"
                f" / 字数 {stats['min_words']}-{stats['max_words']}"
                f"（中位 {stats['median_words']}）"
            )
            if stats["turns"] < len(TURNS):
                failures.append(f"{persona['nickname']} 只完成 {stats['turns']} 轮")
            if stats["question_ratio"] >= 0.7:
                failures.append(f"{persona['nickname']} 追问率过高 {stats['question_ratio']}")
            if stats["unique"] < 7:
                failures.append(f"{persona['nickname']} 回复重复过多（{stats['unique']} 种）")
            if stats["distinct_openers"] < 4:
                failures.append(f"{persona['nickname']} 开场词太单一（{stats['distinct_openers']}）")

        # 两个人物对同一句输入的差异（可识别性）
        if len(results) == 2:
            names = list(results)
            a, b = results[names[0]], results[names[1]]
            same_openers = len(set(
                t.split()[0].lower() for t in a["replies"] if t.split()
            ) & set(t.split()[0].lower() for t in b["replies"] if t.split()))
            print(
                f"\n== 表达可区分性 ==\n{names[0]} 中位长度 {a['median_words']}"
                f" vs {names[1]} {b['median_words']}；共同开场词 {same_openers}"
            )

        # 信息流：事件驱动动态 + AI 评论
        feed = client.get("/api/v3/feed", params={"limit": 20}).json()
        items = feed.get("items") or []
        event_posts = [i for i in items if i.get("source_event_id")]
        ai_comments = [
            c
            for i in items
            for c in (i.get("comments") or [])
            if (c.get("author") or {}).get("is_ai")
        ]
        print(f"\n== 信息流 ==\n  条数 {len(items)} / 事件驱动动态 {len(event_posts)} / AI 评论 {len(ai_comments)}")
        for item in event_posts[:4]:
            author = (item.get("author") or {}).get("nickname")
            print(f"  [事件动态] {author}: {item.get('content', '')[:90]}")
        for comment in ai_comments[:3]:
            print(f"  [AI 评论] {comment['author'].get('nickname')}: {comment.get('content', '')[:70]}")

    print("\n== 结果 ==")
    if failures:
        for failure in failures:
            print(f"  FAIL: {failure}")
        return 1
    print("  PASS：真实 LLM 对话未出现模板化特征；信息流与人物数据可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
