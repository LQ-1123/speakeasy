"""临时脚本：在真实库副本上验证幂等迁移（不修改 data/speakeasy_v2.db）。"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.v2.db import DB  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402

SRC = os.path.join(ROOT, "data", "speakeasy_v2.db")
target = os.path.join(tempfile.mkdtemp(prefix="mig-"), "copy.db")
shutil.copyfile(SRC, target)

db = DB(target)
db.init()
ensure_schema(db, seed=False)
ensure_schema(db, seed=False)  # 幂等：连跑两次
print("migration OK ->", target)

for table in ("personas", "persona_posts", "persona_post_comments", "friends"):
    cols = [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()]
    wanted = [
        c
        for c in (
            "speech_dna", "emotion_state", "source_event_id", "post_type", "published_at",
            "metadata", "author_persona_id", "author_type", "recent_behaviors", "persona_id",
        )
        if c in cols
    ]
    print(f"{table}: {wanted}")

indexes = [
    r[0]
    for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uq_%'"
    ).fetchall()
]
print("unique indexes:", indexes)

tables = [
    r[0]
    for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'persona_%'"
    ).fetchall()
]
print("persona tables:", sorted(tables))

# 老数据必须还在
print("personas rows:", db.conn.execute("SELECT COUNT(*) FROM personas").fetchone()[0])
print("posts rows:", db.conn.execute("SELECT COUNT(*) FROM persona_posts").fetchone()[0])
db.close()
