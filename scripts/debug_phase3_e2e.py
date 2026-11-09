"""临时脚本：检查第三阶段关系张力链路（事件完成 → tension/shared 变化）。"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(os.path.join(ROOT, "data", "speakeasy_v2.db"), timeout=15)
conn.row_factory = sqlite3.Row

event = conn.execute(
    "SELECT id, event_type, event_status, participants, completed_at"
    " FROM persona_events WHERE source_reference='phase3-e2e'"
).fetchone()
print("E2E conflict 事件:", dict(event) if event else "未找到")

print("\n有张力的关系（tension > 0）:")
rows = conn.execute(
    "SELECT persona_a, persona_b, closeness, shared_events, tension, last_conflict_at"
    " FROM persona_relationships WHERE tension > 0 ORDER BY tension DESC LIMIT 6"
).fetchall()
if not rows:
    print("  （还没有）")
for row in rows:
    a = conn.execute("SELECT nickname FROM personas WHERE id=?", (row["persona_a"],)).fetchone()
    b = conn.execute("SELECT nickname FROM personas WHERE id=?", (row["persona_b"],)).fetchone()
    print(
        f"  {a['nickname'] if a else row['persona_a']} × {b['nickname'] if b else row['persona_b']}:"
        f" closeness={row['closeness']} shared={row['shared_events']} tension={row['tension']}"
        f" last_conflict={(row['last_conflict_at'] or '-')[:19]}"
    )

print("\n最近完成的事件（含 conflict）:")
for row in conn.execute(
    "SELECT persona_id, event_type, title, completed_at FROM persona_events"
    " WHERE event_status='completed' ORDER BY completed_at DESC LIMIT 5"
):
    print(f"  {row['completed_at'][:19]} {row['event_type']:<16} {row['title'][:40]}")
conn.close()
