"""验收前快照：今天（本地日）的人物生活事件 / 动态 / 评论概况。"""

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
yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))


def one(sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


print(f"== {today} 的生活 ==")
print("  今天事件:", one("SELECT COUNT(*) FROM persona_events WHERE day=?", (today,)))
print("  今日叙事(LLM):", one(
    "SELECT COUNT(*) FROM persona_events WHERE day=? AND source_type='llm'", (today,)))
print("  今日规则事件:", one(
    "SELECT COUNT(*) FROM persona_events WHERE day=? AND source_type='rule'", (today,)))
print("  今日人物数:", one(
    "SELECT COUNT(DISTINCT persona_id) FROM persona_events WHERE day=?", (today,)))
print("  今日已完成:", one(
    "SELECT COUNT(*) FROM persona_events WHERE day=? AND event_status='completed'", (today,)))

print(f"\n== 近 24h 内容 ==")
print("  人物动态:", one(
    "SELECT COUNT(*) FROM persona_posts WHERE created_at >= ?",
    (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400)) + "+00:00",)))
print("  其中事件驱动:", one(
    "SELECT COUNT(*) FROM persona_posts WHERE source_event_id<>''"))
print("  AI 人物评论:", one("SELECT COUNT(*) FROM persona_post_comments WHERE author_type='persona'"))

print("\n== 今天的事件样例（叙事） ==")
for row in conn.execute(
    "SELECT p.nickname, e.event_type, e.title, e.event_status FROM persona_events e"
    " JOIN personas p ON p.id = e.persona_id"
    " WHERE e.day=? AND e.source_type='llm' ORDER BY e.start_time LIMIT 6", (today,)
):
    print(f"  [{row['event_status']}] {row['nickname']:<18} {row['event_type']:<14} {row['title'][:40]}")

print("\n== 昨天 vs 今天 ==")
print("  昨天事件:", one("SELECT COUNT(*) FROM persona_events WHERE day=?", (yesterday,)))
print("  今天事件:", one("SELECT COUNT(*) FROM persona_events WHERE day=?", (today,)))
conn.close()
