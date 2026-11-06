import sqlite3

c = sqlite3.connect("data/speakeasy_v2.db")
rows = c.execute(
    "select p.nickname, po.content, po.media, po.source_ref, po.post_kind"
    " from persona_posts po join personas p on p.id=po.persona_id"
    " where po.post_kind='life_photo' order by po.created_at desc limit 8"
).fetchall()
print("life_photo posts:", len(rows))
for nick, content, media, ref, kind in rows:
    print("-", nick, "|", content[:60].replace("\n", " "), "|", media, "|", str(ref)[:90])
print("\nmedia_library rows:", c.execute("select count(*) from media_library").fetchone()[0])
print("text-only posts:", c.execute(
    "select count(*) from persona_posts where media='[]' and expires_at<>''"
).fetchone()[0])
