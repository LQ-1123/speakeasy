import sqlite3

c = sqlite3.connect("data/speakeasy_v2.db")
rows = c.execute(
    "SELECT id,nickname,lifecycle,gender,language,fish_voice_id FROM personas"
).fetchall()
assigned = [r for r in rows if r[5]]
print("total personas:", len(rows), "assigned:", len(assigned), "unique:", len({r[5] for r in assigned}))
from collections import Counter

print("lifecycle:", Counter(r[2] for r in rows))
for r in assigned[:12]:
    print(r[1], "|", r[2] or "-", "|", r[3], "|", r[4], "|", r[5][:14])
