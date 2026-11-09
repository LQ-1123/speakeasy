"""Persona 生活层本地冒烟脚本（不调用真实 LLM）。

用途：在临时库上验证「种子 → Speech DNA / 示例 → 生活事件 → 聊天 persona 层」
整条链路能跑通，且**一次聊天只需要 0 次模型调用**。

    python scripts/smoke_persona_upgrade.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "backend"))

    from app.v2.db import DB  # noqa: PLC0415
    from app.v3 import persona_context  # noqa: PLC0415
    from app.v3.life_events import (  # noqa: PLC0415
        current_activity,
        generate_day_events,
        refresh_statuses,
        upcoming_events,
    )
    from app.v3.schema import ensure_schema  # noqa: PLC0415
    from app.v3.speech_dna import ensure_persona_dna, load_examples  # noqa: PLC0415

    workdir = tempfile.mkdtemp(prefix="speakeasy-persona-")
    db = DB(os.path.join(workdir, "smoke.db"))
    db.init()
    ensure_schema(db, seed=True)

    rows = [dict(r) for r in db.conn.execute("SELECT * FROM personas ORDER BY id").fetchall()]
    print(f"[1] 种子人物: {len(rows)}")

    # --- Speech DNA + 示例对话（确定性兜底，无 LLM） ---
    stats = asyncio.run(ensure_persona_dna(db, None, use_llm=False))
    print(f"[2] ensure_persona_dna: {stats}")

    dna_table = []
    for row in rows:
        dna = json.loads(row.get("speech_dna") or "{}")
        dna_table.append(
            (
                row["nickname"],
                dna.get("humor_style"),
                dna.get("verbosity"),
                dna.get("directness"),
                dna.get("conversation_initiative"),
            )
        )
    print("[3] 12 人物表达配置（humor / verbosity / directness / initiative）:")
    for item in dna_table:
        print("    ", " | ".join(str(x) for x in item))

    ex = load_examples(db, rows[0]["id"])
    print(f"[4] 示例对话: {rows[0]['nickname']} 有 {len(ex)} 组，场景 {sorted({r['scene'] for r in ex})}")

    # --- 生活事件（规则引擎，无 LLM） ---
    ev = asyncio.run(generate_day_events(db, None))
    print(f"[5] 今日生活事件: {ev}")
    changed = refresh_statuses(db)
    print(f"[6] 状态推进: {changed} 条")
    for row in rows[:3]:
        act = current_activity(db, row["id"])
        up = upcoming_events(db, row["id"], limit=2)
        print(
            f"    {row['nickname']}: 正在={((act or {}).get('label')) or '-'} "
            f"计划={[u.get('title') for u in up]}"
        )

    # --- 聊天 persona 层 ---
    target = rows[0]
    friend = db.create_friend(
        {
            "name": target["nickname"],
            "persona_id": target["id"],
            "city": target.get("city") or "",
            "nationality": target.get("country") or "",
            "native_lang": target.get("language") or "en",
            "reply_language": target.get("language") or "en",
            "personality_prompt": (target.get("personality") or "")[:500],
            "speech_quirks": ["right"],
            "closeness_level": 3,
            "total_turns": 35,
        }
    )
    plan = persona_context.build_turn_plan(db, friend, "Hey, how was your weekend?")
    print(
        f"[7] TurnPlan: behavior={plan.behavior} scene={plan.scene} "
        f"examples={len(plan.examples)} life_facts={bool(plan.life_block)} "
        f"mood={plan.emotion.get('mood')} energy={plan.emotion.get('energy')}"
    )
    layer = persona_context.build_persona_layer(db, friend, plan, language="en")
    print(f"[8] persona 层 {len(layer)} 字符:")
    print("-" * 68)
    print(layer)
    print("-" * 68)

    # --- 行为历史写回（下一轮不会重复同一种模式） ---
    recent = persona_context.record_behavior(db, friend, plan.behavior)
    intent = __import__("app.v3.behavior", fromlist=["detect_intent"]).detect_intent(
        "Hey, how was your weekend?"
    )
    state = persona_context.update_emotion_after_turn(
        db, plan, intent=intent, behavior_value=plan.behavior
    )
    print(f"[9] recent_behaviors={recent} emotion={state.get('mood')}/{state.get('energy')}")

    # --- 再次构建：应该能看到行为历史 ---
    friend2 = db.get_friend(friend["id"])
    plan2 = persona_context.build_turn_plan(db, friend2, "Anyway, tell me something about your day")
    print(f"[10] 第二轮 behavior={plan2.behavior} reason={plan2.behavior_reason}")

    db.close()
    print("\nOK：Persona 生活层冒烟通过（0 次 LLM 调用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
