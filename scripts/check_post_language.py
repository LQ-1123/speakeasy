import sqlite3

c = sqlite3.connect("data/speakeasy_v2.db")
rows = c.execute("select language, count from (select language, count(*) as count from persona_posts group by language)").fetchall()
print("posts by language:", rows)
for r in c.execute("select id, language, persona_id from persona_posts where persona_id in ('p_lin','p_jiwoo','p_giulia')").fetchall():
    print(r)
