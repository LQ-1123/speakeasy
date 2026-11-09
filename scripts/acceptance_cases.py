"""验收用：给每个常驻人物输出「今天已完成 / 计划中」的事件，方便设计验收提问。"""

import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(os.path.join(ROOT, "data", "speakeasy_v2.db"), timeout=15)
conn.row_factory = sqlite3.Row
today = time.strftime("%Y-%m-%d")
now = time.strftime("%H:%M")

focus = ("Callum", "Lin", "Haruto", "Jiwoo", "Camila", "Adrian", "Niamh")
print(f"本地时间 {now} · 日 {today}\n")
for nickname in focus:
    person = conn.execute(
        "SELECT id, nickname, language, occupation FROM personas WHERE nickname=?", (nickname,)
    ).fetchone()
    if person is None:
        continue
    print(f"== {person['nickname']}（{person['occupation']}，{person['language']}）==")
    rows = conn.execute(
        "SELECT title, event_type, event_status, start_time, end_time FROM persona_events"
        " WHERE persona_id=? AND day=? ORDER BY start_time",
        (person["id"], today),
    ).fetchall()
    done = [r for r in rows if r["event_status"] == "completed"]
    planned = [r for r in rows if r["event_status"] in ("planned", "ongoing")]
    for row in done[:2]:
        print(f"  已完成: {row['start_time'][11:16]}-{row['end_time'][11:16]} {row['title'][:44]}")
    for row in planned[:2]:
        print(f"  进行/计划: {row['start_time'][11:16]}-{row['end_time'][11:16]} {row['title'][:44]}")
    if not done and not planned:
        print("  （今天没有事件）")
    print()
conn.close()
