import sqlite3
import json

c = sqlite3.connect("data/speakeasy_v2.db")
rows = c.execute("select id, avatar_style, avatar_options from personas order by id").fetchall()
with_style = 0
for pid, style, raw in rows:
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    if "style" in data:
        with_style += 1
    print(pid, style, "keys:", len(data), "style_key:", "style" in data, "top:", data.get("top"))
print("rows still containing style:", with_style)
