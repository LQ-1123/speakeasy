"""清空 V3 联调期间产生的测试数据（保留 Persona 语料和 local 用户的历史数据）。

用法：python scripts/reset_v3_test_data.py [--db data/speakeasy_v2.db]
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import shutil


def main() -> int:
    db_path = "data/speakeasy_v2.db"
    args = sys.argv[1:]
    if "--db" in args:
        db_path = args[args.index("--db") + 1]
    if not os.path.isfile(db_path):
        print(f"数据库不存在: {db_path}")
        return 1

    backup = f"{db_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(db_path, backup)
    print(f"备份: {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    deleted: dict[str, int] = {}

    def run(sql: str, params: tuple = ()) -> None:
        cur = conn.execute(sql, params)
        deleted[sql.split()[0].upper() + " " + sql.split()[1] if len(sql.split()) > 1 else sql] = cur.rowcount

    with conn:
        # 测试产生的用户身份
        run("DELETE FROM v3_users WHERE id <> 'local'")
        # 用户动态与其互动
        run("DELETE FROM user_post_likes")
        run("DELETE FROM user_post_comments")
        run("DELETE FROM user_posts")
        # 社交事件
        run("DELETE FROM social_events")
        run("DELETE FROM social_event_attempts")
        # 测试用户的 Persona 互动
        run("DELETE FROM persona_post_likes WHERE user_id <> 'local'")
        run("DELETE FROM persona_post_comments WHERE user_id <> 'local'")
        # 好友关系 + 测试用户的好友行（级联删除消息）
        run("DELETE FROM friendships")
        friends = conn.execute(
            "SELECT id FROM friends WHERE COALESCE(owner_user_id,'local') <> 'local'"
        ).fetchall()
        for row in friends:
            conn.execute("DELETE FROM friends WHERE id=?", (row["id"],))
        deleted["DELETE friends(non-local)"] = len(friends)

    counts = {
        "personas": conn.execute("SELECT COUNT(*) c FROM personas").fetchone()["c"],
        "persona_posts": conn.execute("SELECT COUNT(*) c FROM persona_posts").fetchone()["c"],
        "friends_local": conn.execute(
            "SELECT COUNT(*) c FROM friends WHERE COALESCE(owner_user_id,'local')='local'"
        ).fetchone()["c"],
    }
    conn.close()
    print("删除:", deleted)
    print("保留:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
