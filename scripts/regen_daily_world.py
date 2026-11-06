"""重新生成今天的每日世界（清掉今天已生成的游客，再按新 prompt 生成）。"""

import asyncio
import sys

sys.path.insert(0, "backend")

from app.config import get_settings  # noqa: E402
from app.engines import build_engines  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v3.daily_world import day_key, generate_daily  # noqa: E402
import os  # noqa: E402


async def main():
    settings = get_settings()
    db_path = os.path.join(str(settings.data_path), "speakeasy_v2.db")
    db = DB(db_path)
    db.init()
    day = day_key()
    with db._lock, db.conn:  # noqa: SLF001
        guests = db.conn.execute(
            "SELECT id FROM personas WHERE lifecycle='guest' AND ai_config LIKE ?",
            (f'%"day": "{day}"%',),
        ).fetchall()
        for row in guests:
            db.conn.execute("DELETE FROM persona_posts WHERE persona_id=?", (row["id"],))
            db.conn.execute("DELETE FROM personas WHERE id=?", (row["id"],))
        db.conn.execute("DELETE FROM daily_world WHERE day=?", (day,))
    print("cleared guests:", len(guests), "for", day)

    engines = build_engines(settings)
    result = await generate_daily(db, engines.llm, day=day, force=True)
    print("regenerated:", result)
    await engines.aclose()
    db.close()


asyncio.run(main())
