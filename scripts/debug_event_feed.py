"""临时脚本：检查事件驱动动态为什么没出现在信息流里。"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = os.path.join(ROOT, "data", "speakeasy_v2.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print("== 事件驱动动态 ==")
for row in conn.execute(
    "SELECT id, persona_id, substr(content,1,70) AS c, language, visibility, created_at,"
    " expires_at, published_at, post_type, source_event_id, metadata"
    " FROM persona_posts WHERE source_event_id <> ''"
):
    print(dict(row))

print("\n== 该动态的评论 ==")
for row in conn.execute(
    "SELECT post_id, author_type, author_persona_id, substr(content,1,60) FROM persona_post_comments"
    " WHERE post_id IN (SELECT id FROM persona_posts WHERE source_event_id <> '')"
):
    print(dict(row))

print("\n== 最近 8 条 persona_posts（含 expires_at） ==")
for row in conn.execute(
    "SELECT id, persona_id, visibility, created_at, expires_at, post_type,"
    " CASE WHEN source_event_id='' THEN '-' ELSE source_event_id END AS ev"
    " FROM persona_posts ORDER BY created_at DESC LIMIT 8"
):
    print(dict(row))

print("\n== 用户 target_languages ==")
row = conn.execute("SELECT target_languages FROM user_profile WHERE id=1").fetchone()
print(row["target_languages"] if row else None)

print("\n== 已完成的最近事件（posts 候选） ==")
for row in conn.execute(
    "SELECT id, persona_id, title, event_status, start_time, end_time, completed_at"
    " FROM persona_events WHERE event_status='completed'"
    " ORDER BY COALESCE(NULLIF(completed_at,''), start_time) DESC LIMIT 5"
):
    print(dict(row))
