"""修复历史音色数据：一人一色 + 同步到好友行。

背景：早期 `assign_fish_voices` 的去重集合在循环外算，导致同批角色可能拿到同一个
Fish 音色；另有角色落到账号全局参考音色（e3cd…）。Fish 恢复后跑一次即可修复。

    python scripts/repair_persona_voices.py
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.seed_data import ensure_persona_voices  # noqa: E402
from app.voice import voice_registry  # noqa: E402


def report(db: DB, title: str) -> None:
    rows = db.conn.execute(
        "SELECT nickname, language, gender, fish_voice_id FROM personas ORDER BY nickname"
    ).fetchall()
    voices = [str(r["fish_voice_id"] or "") for r in rows]
    filled = [v for v in voices if v]
    dupes: dict[str, list[str]] = {}
    for row in rows:
        key = str(row["fish_voice_id"] or "")
        if key:
            dupes.setdefault(key, []).append(str(row["nickname"]))
    shared = {k: v for k, v in dupes.items() if len(v) > 1}
    cfg = voice_registry.load_cfg()
    global_ref = str(getattr(cfg, "fish_reference_id", "") or "")
    using_global = [str(r["nickname"]) for r in rows if str(r["fish_voice_id"] or "") == global_ref]
    print(f"\n== {title} ==")
    print(f"  人物 {len(rows)}，有 Fish 音色 {len(filled)}，唯一值 {len(set(filled))}")
    print(f"  重复占用同一音色: {shared or '无'}")
    print(f"  仍使用账号全局参考音色: {using_global or '无'}")


def main() -> int:
    path = os.path.join(str(get_settings().data_path), "speakeasy_v2.db")
    db = DB(path)
    db.init()
    ensure_schema(db)

    report(db, "修复前")
    assigned = asyncio.run(ensure_persona_voices(db))
    print(f"\n本次分配/修复: {assigned} 个")
    report(db, "修复后")

    # 好友行同步检查
    rows = db.conn.execute(
        "SELECT f.name, f.fish_voice_id AS fv, p.fish_voice_id AS pv"
        " FROM friends f JOIN personas p ON p.id = f.persona_id"
    ).fetchall()
    mismatch = [
        r["name"] for r in rows if str(r["fv"] or "") != str(r["pv"] or "")
    ]
    print(f"\n== 好友行同步 ==\n  {len(rows)} 个好友，音色与人物不一致: {mismatch or '无'}")

    # 当前 Fish 可用性
    status = voice_registry.fish_status()
    print(f"\n== Fish 状态 ==\n  {status}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
