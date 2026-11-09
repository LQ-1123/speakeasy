"""临时脚本：检查事件驱动动态的多视角与 AI 评论（真实库）。"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(os.path.join(ROOT, "data", "speakeasy_v2.db"))
conn.row_factory = sqlite3.Row

print("== 一个事件 → 多条动态（多视角）==")
rows = conn.execute(
    "SELECT pp.source_event_id AS ev, COUNT(*) AS n, GROUP_CONCAT(p.nickname, ' / ') AS who"
    " FROM persona_posts pp JOIN personas p ON p.id = pp.persona_id"
    " WHERE pp.source_event_id <> '' GROUP BY pp.source_event_id"
    " HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 5"
).fetchall()
for row in rows:
    print(f"  事件 {row['ev']} → {row['n']} 条，作者：{row['who']}")

print("\n== 事件驱动动态明细 ==")
for row in conn.execute(
    "SELECT pp.source_event_id AS ev, p.nickname AS who, p.language AS lang,"
    " substr(pp.content, 1, 90) AS c, json_extract(pp.metadata, '$.event_title') AS title"
    " FROM persona_posts pp JOIN personas p ON p.id = pp.persona_id"
    " WHERE pp.source_event_id <> '' ORDER BY pp.created_at DESC LIMIT 12"
):
    print(f"  [{row['ev']}] {row['who']}({row['lang']}): {row['c']}")
    print(f"      事件标题: {row['title']}")

print("\n== AI 人物评论 ==")
for row in conn.execute(
    "SELECT c.post_id, c.author_persona_id, p.nickname, substr(c.content,1,80) AS c,"
    " c.created_at FROM persona_post_comments c"
    " LEFT JOIN personas p ON p.id = c.author_persona_id"
    " WHERE c.author_type = 'persona' ORDER BY c.created_at DESC LIMIT 8"
):
    print(f"  [{row['post_id']}] {row['nickname']}: {row['c']}")

print("\n== 事件评估标记分布 ==")
for row in conn.execute(
    "SELECT COUNT(*) AS n, SUM(json_extract(metadata, '$.posts_generated')) AS posts"
    " FROM persona_events WHERE metadata LIKE '%posts_evaluated_at%'"
):
    print(f"  已评估事件 {row['n']} 条，累计产出动态 {row['posts']} 条")
