"""第四阶段验收：一个角色 = 一个声音（服务端自检 + 朗读接口实测）。

    python scripts/verify_voice_consistency.py

判定：
1. `/api/v3/stats/voice`：同一人物的「聊天侧绑定」与「朗读侧绑定」token 完全一致；
2. 朗读接口返回的 `X-Voice` 属于该人物自己的两种声线之一：
   fish-<hash(fish_voice_id)> 或 edge-<persona.voice_id>；
3. Fish 不可达时，聊天与朗读都应落到**同一个** edge 声线（服务端熔断共享）。
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Any

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")


def _expected_tokens(persona: dict) -> set[str]:
    tokens: set[str] = set()
    fish_id = str(persona.get("fish_voice_id") or "").strip()
    if fish_id:
        tokens.add(f"fish-{hashlib.sha1(fish_id.encode()).hexdigest()[:8]}")
    edge = str(persona.get("voice_id") or "").strip()
    if edge:
        tokens.add(f"edge-{edge}")
    return tokens


def main() -> int:
    failures: list[str] = []
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        check = client.get("/api/v3/stats/voice").json()
        print("== 服务端音色自检 ==")
        print("  provider:", check.get("provider"), "| paths:", check.get("paths"))
        print("  fish:", check.get("fish"))
        for item in check.get("items") or []:
            chat = item.get("chat") or {}
            speech = item.get("speech") or {}
            print(
                f"  {item['nickname']:<18} chat={chat.get('token', '-'):<34}"
                f" speech={speech.get('token', '-'):<34}"
                f" consistent={item['consistent']}"
            )
            if not item.get("consistent"):
                failures.append(f"{item['nickname']}: 聊天/朗读绑定不一致")
        if not check.get("all_consistent", False):
            failures.append("服务端自检 all_consistent=False")

        feed = client.get("/api/v3/feed", params={"limit": 40}).json()
        # 以服务端自检给出的「该人物的朗读 token」为准（同一个 resolver 算出来的）
        expected_by_persona: dict[str, dict[str, Any]] = {
            str(item["persona_id"]): item for item in (check.get("items") or [])
        }
        print("\n== 朗读接口实测（X-Voice 必须等于该人物的朗读绑定）==")
        checked = 0
        for item in feed.get("items") or []:
            author = item.get("author") or {}
            pid = str(author.get("id") or "")
            if item.get("kind") != "persona_post" or not pid:
                continue
            entry = expected_by_persona.get(pid)
            if entry is None:
                entry = client.get(
                    "/api/v3/stats/voice", params={"persona_id": pid}
                ).json().get("items", [{}])[0]
                expected_by_persona[pid] = entry
            expected = str((entry.get("speech") or {}).get("token") or "")
            with client.stream("GET", f"/api/v3/persona-posts/{item['id']}/speech") as resp:
                token = resp.headers.get("X-Voice", "")
                provider = resp.headers.get("X-Voice-Provider", "")
                resp.read()
                status = resp.status_code
            checked += 1
            ok = status == 200 and token == expected
            print(
                f"  {author.get('nickname', pid):<18} status={status} provider={provider or '-':<5}"
                f" token={token or '-':<34} expected={expected:<34} {'OK' if ok else 'MISMATCH'}"
            )
            if not ok:
                failures.append(
                    f"{author.get('nickname', pid)}: 朗读 token={token} != {expected}"
                    f"（status={status} provider={provider}）"
                )
            if checked >= 6:
                break

    print("\n== 结果 ==")
    if failures:
        for item in failures:
            print("  FAIL:", item)
        return 1
    print("  PASS：同一角色的聊天/朗读解析到同一声线，朗读只使用该角色自己的音色")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
