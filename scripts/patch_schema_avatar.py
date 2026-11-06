"""一次性迁移：保证 personas 有 avatar_seed 列，并把种子头像写进 upsert。"""

import io

p = "backend/app/v3/schema.py"
s = io.open(p, encoding="utf-8").read()

old = '''                "INSERT INTO personas (id, nickname, avatar, bio, personality, interests,"
                " language, country, city, timezone, occupation, age, gender, voice_id,"
                " archetype_id, ai_config, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET nickname=excluded.nickname, avatar=excluded.avatar,"'''
new = '''                "INSERT INTO personas (id, nickname, avatar, avatar_seed, bio, personality,"
                " interests, language, country, city, timezone, occupation, age, gender,"
                " voice_id, archetype_id, ai_config, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET nickname=excluded.nickname,"
                " avatar=excluded.avatar, avatar_seed=excluded.avatar_seed,"'''
assert old in s, "insert block not found"
s = s.replace(old, new)

old2 = '''                    p["id"], p["nickname"], p["avatar"], p["bio"], p["personality"],'''
new2 = '''                    p["id"], p["nickname"], "", p.get("avatar_seed", ""),
                    p["bio"], p["personality"],'''
assert old2 in s, "values tuple not found"
s = s.replace(old2, new2)

old4 = '''        pcols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(personas)").fetchall()
        }
        if pcols and "avatar_seed" not in pcols:'''
new4 = '''        pcols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(personas)").fetchall()
        }
        if pcols and "avatar_seed" not in pcols:'''
if old4 not in s:
    anchor = '''        if cols and "language" not in cols:
            db.conn.execute(
                "ALTER TABLE persona_posts ADD COLUMN language TEXT NOT NULL DEFAULT \'\'"
            )
            log.info("migrated persona_posts: added language column")'''
    assert anchor in s, "language migration block not found"
    addition = anchor + '''

        pcols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(personas)").fetchall()
        }
        if pcols and "avatar_seed" not in pcols:
            db.conn.execute(
                "ALTER TABLE personas ADD COLUMN avatar_seed TEXT NOT NULL DEFAULT \'\'"
            )
            log.info("migrated personas: added avatar_seed column")'''
    s = s.replace(anchor, addition)

io.open(p, "w", encoding="utf-8").write(s)
print("schema.py patched")
