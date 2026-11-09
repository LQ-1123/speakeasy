"""临时脚本：检查第二阶段 E2E 事件是否被后台完成、关系是否 bump。"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(os.path.join(ROOT, "data", "speakeasy_v2.db"))
conn.row_factory = sqlite3.Row

row = conn.execute(
    "SELECT id, event_status, start_time, end_time, completed_at, participants"
    " FROM persona_events WHERE source_reference='phase2-e2e'"
).fetchone()
print("E2E 事件:", dict(row) if row else "未找到")

print("\nongoing 事件（最近 8 条）:")
for r in conn.execute(
    "SELECT persona_id, event_type, title, start_time, end_time FROM persona_events"
    " WHERE event_status='ongoing' ORDER BY start_time DESC LIMIT 8"
):
    print(f"  {r['start_time'][11:16]} → {r['end_time'][11:16]} {r['event_type']:<14} {r['title'][:34]}")

print("\n关系（按 shared_events 降序）:")
for r in conn.execute(
    "SELECT persona_a, persona_b, closeness, shared_events, last_shared_at"
    " FROM persona_relationships ORDER BY shared_events DESC, closeness DESC LIMIT 8"
):
    print(
        f"  {r['persona_a']} - {r['persona_b']}: closeness={r['closeness']}"
        f" shared={r['shared_events']} last={(r['last_shared_at'] or '-')[:19]}"
    )
