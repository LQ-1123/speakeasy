"""重新生成今天的每日世界（含图库生活帖）。"""

import asyncio
import os
import sys

sys.path.insert(0, "backend")

from app.config import get_settings  # noqa: E402
from app.engines import build_engines  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v3.daily_world import day_key, generate_daily  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402


async def main():
    settings = get_settings()
    db_path = os.path.join(str(settings.data_path), "speakeasy_v2.db")
    db = DB(db_path)
    db.init()
    ensure_schema(db, seed=False)

    day = day_key()
    with db._lock, db.conn:  # noqa: SLF001
        guests = db.conn.execute(
            "SELECT id FROM personas WHERE lifecycle IN ('guest','adopted')"
        ).fetchall()
        for row in guests:
            db.conn.execute("DELETE FROM persona_posts WHERE persona_id=?", (row["id"],))
            db.conn.execute("DELETE FROM personas WHERE id=?", (row["id"],))
        db.conn.execute("DELETE FROM daily_world WHERE day=?", (day,))
    print("cleared guests:", len(guests), "for", day)

    engines = build_engines(settings)
    media_dir = os.path.join(str(settings.data_path), "media")
    result = await generate_daily(db, engines.llm, day=day, force=True, media_dir=media_dir)
    print("regenerated:", result)
    await engines.aclose()
    db.close()


asyncio.run(main())
