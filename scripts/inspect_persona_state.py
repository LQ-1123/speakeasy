"""临时脚本：检查本地运行库的 Persona 生活系统数据状态。"""

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "speakeasy_v2.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def count(table: str) -> object:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as exc:
        return f"ERR {exc}"


print("== 表行数 ==")
for table in (
    "personas",
    "persona_posts",
    "persona_events",
    "persona_schedules",
    "persona_relationships",
    "persona_example_dialogs",
    "daily_world",
):
    print(f"  {table}: {count(table)}")

print("\n== 生活系统关键指标 ==")
scalar_queries = [
    ("事件驱动动态", "SELECT COUNT(*) FROM persona_posts WHERE source_event_id <> ''"),
    ("AI 人物评论", "SELECT COUNT(*) FROM persona_post_comments WHERE author_type = 'persona'"),
    ("已完成事件", "SELECT COUNT(*) FROM persona_events WHERE event_status = 'completed'"),
    ("计划事件", "SELECT COUNT(*) FROM persona_events WHERE event_status = 'planned'"),
    ("已评估事件", "SELECT COUNT(*) FROM persona_events WHERE metadata LIKE '%posts_evaluated%'"),
    ("有情绪状态的人物", "SELECT COUNT(*) FROM personas WHERE emotion_state NOT IN ('', '{}')"),
]
for label, sql in scalar_queries:
    try:
        print(f"  {label}: {conn.execute(sql).fetchone()[0]}")
    except sqlite3.Error as exc:
        print(f"  {label}: ERR {exc}")

print("\n== v3_meta ==")
for row in conn.execute("SELECT key, value FROM v3_meta ORDER BY key"):
    print(f"  {row['key']} = {row['value'][:80]}")

print("\n== personas 表达层 ==")
for row in conn.execute(
    "SELECT nickname, lifecycle, language, length(speech_dna) AS dna_len,"
    " length(emotion_state) AS emo_len FROM personas ORDER BY id"
):
    print(
        f"  {row['nickname']:<10} {row['lifecycle']:<10} lang={row['language']:<3}"
        f" dna={row['dna_len']:<4} emotion={row['emo_len']}"
    )

print("\n== 示例对话（按人物） ==")
for row in conn.execute(
    "SELECT p.nickname, COUNT(d.id) AS n, GROUP_CONCAT(DISTINCT d.language) AS langs"
    " FROM personas p LEFT JOIN persona_example_dialogs d ON d.persona_id=p.id"
    " GROUP BY p.id ORDER BY p.id"
):
    print(f"  {row['nickname']:<10} examples={row['n']:<3} langs={row['langs']}")

print("\n== 生活事件（最近 10 条） ==")
for row in conn.execute(
    "SELECT persona_id, event_type, title, start_time, end_time, event_status, source_type"
    " FROM persona_events ORDER BY start_time DESC LIMIT 10"
):
    print(
        f"  [{row['event_status']:<9}] {row['event_type']:<14} {row['title'][:38]:<40}"
        f" {row['start_time'][:16]} → {(row['end_time'] or '-')[:16]} ({row['source_type']})"
    )

print("\n== 事件 → 动态 ==")
for row in conn.execute(
    "SELECT source_event_id, COUNT(*) AS n FROM persona_posts"
    " WHERE source_event_id <> '' GROUP BY source_event_id ORDER BY n DESC LIMIT 10"
):
    print(f"  {row['source_event_id']} -> {row['n']} 条")

print("\n== 人物关系 ==")
for row in conn.execute(
    "SELECT persona_a, persona_b, relation_type, closeness FROM persona_relationships LIMIT 12"
):
    print(f"  {row['persona_a']} -- {row['persona_b']} ({row['relation_type']}, {row['closeness']})")

print("\n== 情绪状态样例 ==")
for row in conn.execute(
    "SELECT nickname, emotion_state FROM personas WHERE emotion_state NOT IN ('', '{}') LIMIT 3"
):
    print(f"  {row['nickname']}: {row['emotion_state'][:160]}")
