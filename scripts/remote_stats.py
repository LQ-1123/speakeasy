"""部署前/后服务器数据基线（只读，不修改任何数据）。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import urllib.request

ROOT = "/root/speakeasy"
DB = os.path.join(ROOT, "data", "speakeasy_v2.db")


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return int(response.status)
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables = [
        "v3_users",
        "user_profiles",
        "user_profile",
        "friends",
        "messages",
        "personas",
        "persona_posts",
        "friendships",
        "user_posts",
        "saved_expressions",
        "social_events",
    ]
    counts: dict[str, object] = {}
    for table in tables:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except Exception as exc:  # noqa: BLE001
            counts[table] = f"err:{exc}"
    for table in ("user_profiles", "user_profile"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        counts[f"{table}.setup_col"] = "profile_setup_completed_at" in cols
        if "profile_setup_completed_at" in cols:
            done = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE COALESCE(profile_setup_completed_at,'') <> ''"
            ).fetchone()["n"]
            counts[f"{table}.setup_completed"] = done
    conn.close()
    try:
        head = subprocess.run(
            ["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        head = "unknown"
    payload = {
        "git_head": head,
        "counts": counts,
        "http": {
            "local_health": http_status("http://127.0.0.1:8000/api/v3/health"),
            "public_home": http_status("https://47-104-24-236.sslip.io/"),
            "public_feed": http_status("https://47-104-24-236.sslip.io/api/v3/feed?limit=1"),
            "public_personas": http_status("https://47-104-24-236.sslip.io/api/v3/personas?limit=1"),
            "public_admin_prompt": http_status(
                "https://47-104-24-236.sslip.io/api/admin/prompt/versions"
            ),
        },
    }
    print("SERVER_STATS " + json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
