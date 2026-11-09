"""临时脚本：向本地运行库插入一条「已结束但未刷新」的 conflict 事件。

用于验证第三阶段真实链路：后台 tick 完成事件 → tension +1、shared_events +1、
`comment_probability` 下降。

    python scripts/seed_live_tension_event.py
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

# 选一对已经存在关系的两个人物（tension 链路只对已有关系生效）
row = conn.execute(
    "SELECT * FROM persona_relationships ORDER BY shared_events DESC, closeness DESC LIMIT 1"
).fetchone()
if row is None:
    print("没有关系数据")
    raise SystemExit(1)

a, b = str(row["persona_a"]), str(row["persona_b"])
names = {}
for pid in (a, b):
    n = conn.execute("SELECT nickname FROM personas WHERE id=?", (pid,)).fetchone()
    names[pid] = n["nickname"] if n else pid

now = time.time()
event_id = f"pe_{uuid.uuid4().hex[:12]}"
payload = {
    "persona_id": a,
    "event_type": "conflict",
    "title": "Phase-3 E2E: argument about the group trip",
    "description": "A short, real argument about who was supposed to book the tickets.",
    "start_time": iso(now - 7200),
    "end_time": iso(now - 3600),
    "participants": json.dumps([b], ensure_ascii=False),
    "location": "",
    "related_personas": "[]",
    "event_status": "ongoing",
    "source_type": "manual",
    "source_reference": "phase3-e2e",
    "visibility": "private",
    "metadata": "{}",
    "dedupe_key": f"phase3-e2e:{a}:{b}:{int(now)}",
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

print("插入 conflict 事件:", event_id)
print(f"  {names[a]}（主体） × {names[b]}（参与者）")
print(f"  关系 before: closeness={row['closeness']} shared={row['shared_events']}"
      f" tension={row['tension']} last_conflict={row['last_conflict_at'] or '-'}")
print("  下一次 refresh_statuses 应：完成事件 → shared+1、tension+1、last_conflict_at 落库")
conn.close()
