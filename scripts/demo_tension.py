"""临时脚本：为「身边有摩擦」的展示造一条数据（同时打印可用的人物 id）。

    python scripts/demo_tension.py set     # 把一对关系 tension 设为 3
    python scripts/demo_tension.py reset   # 还原为 0
"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = os.path.join(ROOT, "data", "speakeasy_v2.db")
action = (sys.argv[1] if len(sys.argv) > 1 else "set").strip().lower()

conn = sqlite3.connect(DB_PATH, timeout=15)
conn.row_factory = sqlite3.Row

# 选一对 closeness>=3 的关系（这样主页会同时满足「亲近」条件）
row = conn.execute(
    "SELECT r.*, a.nickname AS na, b.nickname AS nb FROM persona_relationships r"
    " JOIN personas a ON a.id = r.persona_a JOIN personas b ON b.id = r.persona_b"
    " WHERE r.closeness >= 3 ORDER BY r.shared_events DESC LIMIT 1"
).fetchone()
if row is None:
    print("没有可用的关系")
    raise SystemExit(1)

value = 3 if action == "set" else 0
with conn:
    conn.execute(
        "UPDATE persona_relationships SET tension=?, last_conflict_at=?"
        " WHERE id=?",
        (value, "2026-11-07T14:20:00+00:00" if value else "", row["id"]),
    )
print(f"{action}: {row['na']} × {row['nb']} → tension={value}")
print("主页 URL: http://localhost:8000/#/persona/" + row["persona_a"])
conn.close()
