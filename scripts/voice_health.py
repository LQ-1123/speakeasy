"""体检：Persona 音色分配与好友侧同步情况。"""

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

db = os.path.join(ROOT, "data", "speakeasy_v2.db")
conn = sqlite3.connect(db, timeout=15)
conn.row_factory = sqlite3.Row

cfg_path = os.path.join(ROOT, "data", "tts_config.json")
cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
print("TTS provider:", cfg.get("provider"), "| global ref:", cfg.get("fish_reference_id"))

print("\n== personas 音色 ==")
rows = conn.execute(
    "SELECT id, nickname, language, gender, voice_id, fish_voice_id FROM personas ORDER BY nickname"
).fetchall()
have = 0
for r in rows:
    fish = (r["fish_voice_id"] or "").strip()
    have += 1 if fish else 0
    print(f"  {r['nickname']:<20} {r['language']:<4} {(r['gender'] or '-'):<7}"
          f" edge={(r['voice_id'] or '-'):<28} fish={fish or '（空）'}")
print(f"  → 有 fish 音色的人物 {have}/{len(rows)}")

print("\n== friends 音色（聊天实际读这行）==")
friends = conn.execute(
    "SELECT f.id, f.name, f.persona_id, f.voice_id, f.fish_voice_id, p.fish_voice_id AS p_fish"
    " FROM friends f LEFT JOIN personas p ON p.id = f.persona_id ORDER BY f.created_at"
).fetchall()
for f in friends:
    same = "✓" if (f["fish_voice_id"] or "") == (f["p_fish"] or "") else "✗ 不一致"
    print(f"  {f['name']:<20} persona={f['persona_id'] or '-':<14}"
          f" friend_fish={f['fish_voice_id'] or '（空）':<34}"
          f" persona_fish={f['p_fish'] or '（空）':<34} {same}")

print("\n== 最近音频文件（判断历史消息是否旧音色）==")
for d in ("data/audio", "data/v3_audio/v3"):
    path = os.path.join(ROOT, d)
    if not os.path.isdir(path):
        continue
    names = sorted(
        (os.path.getmtime(os.path.join(path, n)), n)
        for n in os.listdir(path)
        if n.endswith((".mp3", ".wav"))
    )[-6:]
    print(f"  {d}: {len(os.listdir(path))} 个文件")
    for mtime, name in names:
        import time

        print(f"    {time.strftime('%m-%d %H:%M', time.localtime(mtime))} {name}")
conn.close()
