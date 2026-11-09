"""临时脚本：检查多参与者事件的 participants 是否包含主体。"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(os.path.join(ROOT, "data", "speakeasy_v2.db"))
conn.row_factory = sqlite3.Row

print("LLM 叙事事件样例（persona_id / participants）：")
for row in conn.execute(
    "SELECT persona_id, title, participants, event_type FROM persona_events"
    " WHERE source_type = 'llm' ORDER BY start_time DESC LIMIT 10"
):
    print(f"  {row['persona_id']:<22} {row['event_type']:<16} {row['title'][:34]:<36} {row['participants']}")

total = conn.execute(
    "SELECT COUNT(*) FROM persona_events WHERE participants LIKE '%,%'"
).fetchone()[0]
llm_multi = conn.execute(
    "SELECT COUNT(*) FROM persona_events WHERE source_type='llm' AND participants LIKE '%,%'"
).fetchone()[0]
print(f"\n多参与者事件：总 {total} 条，其中 LLM 叙事 {llm_multi} 条")

cols = [r[1] for r in conn.execute("PRAGMA table_info(persona_relationships)").fetchall()]
print("persona_relationships 列:", cols)
