"""在 ensure_world 里补头像种子回填。"""

import io

p = "backend/app/v3/world.py"
s = io.open(p, encoding="utf-8").read()

old_import = """from app.v3.schema import (
    SEED_KEY,
    SEED_VERSION,
    backfill_post_languages,
    seed_if_needed,
    sync_persona_friends,
)"""
new_import = """from app.v3.schema import (
    SEED_KEY,
    SEED_VERSION,
    backfill_avatar_seeds,
    backfill_post_languages,
    seed_if_needed,
    sync_persona_friends,
)"""
assert old_import in s, "import block not found"
s = s.replace(old_import, new_import)

s = s.replace(
    """        sync_persona_friends(db)
        backfill_post_languages(db)
        return world_status(db)""",
    """        sync_persona_friends(db)
        backfill_post_languages(db)
        backfill_avatar_seeds(db)
        return world_status(db)""",
)
s = s.replace(
    """    sync_persona_friends(db)
    backfill_post_languages(db)
    return world_status(db)""",
    """    sync_persona_friends(db)
    backfill_post_languages(db)
    backfill_avatar_seeds(db)
    return world_status(db)""",
)

io.open(p, "w", encoding="utf-8").write(s)
print("backfill_avatar_seeds calls:", s.count("backfill_avatar_seeds(db)"))
