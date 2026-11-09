"""临时脚本：向本地运行库插入一条「已结束但未刷新」的多人事件。

用途：验证后台 `refresh_statuses` 会把它推进为 completed，并给参与者关系 bump
（关系演进是第二阶段的核心链路，只有真实后台 tick 才能证明它接上了）。

    python scripts/seed_live_multi_event.py
"""

import json
import os
import sqlite3
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = os.path.join(ROOT, "data", "speakeasy_v2.db")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


conn = sqlite3.connect(DB_PATH, timeout=15)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, nickname FROM personas WHERE lifecycle <> 'guest' ORDER BY id LIMIT 3"
).fetchall()
if len(rows) < 3:
    print("人物不足")
    raise SystemExit(1)

main, p1, p2 = [dict(r) for r in rows[:3]]
now = time.time()
event_id = f"pe_{uuid.uuid4().hex[:12]}"
dedupe = f"phase2-e2e:{main['id']}:{int(now)}"
payload = {
    "persona_id": main["id"],
    "event_type": "social",
    "title": "Phase-2 E2E: three-person coffee",
    "description": "End-to-end check that a finished multi-person event bumps relationships.",
    "start_time": iso(now - 7200),
    "end_time": iso(now - 3600),
    "participants": json.dumps([p1["id"], p2["id"]], ensure_ascii=False),
    "location": "the usual place",
    "related_personas": "[]",
    "event_status": "ongoing",
    "source_type": "manual",
    "source_reference": "phase2-e2e",
    "visibility": "public",
    "metadata": "{}",
    "dedupe_key": dedupe,
    "day": time.strftime("%Y-%m-%d"),
    "created_at": iso(now - 8000),
    "updated_at": iso(now - 8000),
    "completed_at": "",
}
columns = ",".join(payload)
placeholders = ",".join("?" for _ in payload)
with conn:
    conn.execute(
        f"INSERT INTO persona_events (id,{columns}) VALUES (?,{placeholders})",
        (event_id, *payload.values()),
    )

print("插入事件:", event_id)
print("  主体:", main["nickname"], main["id"])
print("  参与者:", p1["nickname"], p1["id"], "/", p2["nickname"], p2["id"])
print("  状态 ongoing，end_time 已过 → 下一次 refresh_statuses 会完成它")

pairs = [(main["id"], p1["id"]), (main["id"], p2["id"]), (p1["id"], p2["id"])]
print("\n关注这三对关系的 shared_events 变化：")
for a, b in pairs:
    row = conn.execute(
        "SELECT closeness, shared_events FROM persona_relationships"
        " WHERE (persona_a=? AND persona_b=?) OR (persona_a=? AND persona_b=?)",
        (a, b, b, a),
    ).fetchone()
    if row is None:
        print(f"  {a} - {b}: 无关系（不会 bump，符合预期）")
    else:
        print(f"  {a} - {b}: closeness={row['closeness']} shared={row['shared_events']}")
conn.close()
