"""验收：AI 事件与动态 / 世界节奏（管理端热生效 + 模拟今天）。

    python scripts/check_admin_events.py
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")


def show(tag: str, data: object) -> None:
    print(f"{tag}: {json.dumps(data, ensure_ascii=False)}")


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        status = client.get("/api/admin/status").json()
        ok = {i["name"]: i["ok"] for i in status["lexicons"]}
        print("① 词库状态:", ok)
        show("   已生效", status["applied"])

        sim = client.get("/api/admin/simulate").json()
        print(
            "② 模拟今天:",
            f"人物 {sim['world'].get('personas')} · 事件类型 {sim['types'].get('enabled')} 启用 ·",
            f"今天事件 {sim['today'].get('events')} · 动态 {sim['today'].get('posts')} · 评论 {sim['today'].get('comments')}",
        )
        show("   待发估算", sim.get("pending"))

        rhythm = client.get("/api/admin/lexicons/rhythm").json()
        params = rhythm["data"]["params"]
        old_quota = params["event_posts_per_day"]

        # ③ 非法值必须被拒绝
        bad = json.loads(json.dumps(rhythm["data"]))
        bad["params"]["event_posts_per_day"] = 50
        res = client.put("/api/admin/lexicons/rhythm", json={"data": bad}).json()
        print("③ 非法配额被拒绝:", not res.get("ok"), "|", (res.get("issues") or [""])[0][:80])

        # ④ 合法修改 → 热生效（模拟/配额都跟着变）
        good = json.loads(json.dumps(rhythm["data"]))
        good["params"]["event_posts_per_day"] = max(1, min(30, old_quota - 3))
        res = client.put("/api/admin/lexicons/rhythm", json={"data": good}).json()
        sim2 = client.get("/api/admin/simulate").json()
        print(
            "④ 保存配额",
            old_quota,
            "→",
            good["params"]["event_posts_per_day"],
            "| ok:",
            res.get("ok"),
            "| 模拟里的配额:",
            sim2["pending"].get("quota"),
            "| 明天配额:",
            sim2["next_day"].get("posts_quota"),
        )

        # ⑤ 停用一个事件类型 → 运行时类型数减少，但历史事件保留
        events = client.get("/api/admin/lexicons/events").json()
        data = events["data"]
        target = "travel" if "travel" in data["types"] else next(iter(data["types"]))
        data["types"][target]["enabled"] = False
        res = client.put("/api/admin/lexicons/events", json={"data": data}).json()
        applied = res.get("applied") or {}
        print(f"⑤ 停用 {target}: ok={res.get('ok')} | 已生效类型数={applied.get('event_types')}")

        # 回滚
        client.post("/api/admin/lexicons/events/reset")
        client.post("/api/admin/lexicons/rhythm/reset")
        restored = client.get("/api/admin/simulate").json()
        print(
            "⑥ 回滚后配额:",
            restored["pending"].get("quota"),
            "(原",
            old_quota,
            ") | 事件类型:",
            restored["types"].get("enabled"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
