"""Persona 对话质量评估：用真实 LLM 跑 3 种性格 × 多话题，输出可复核的报告。

这不是单元测试，而是「人设是否像真人」的验收探针：
- 每个性格独立注册一个新用户（互不污染），完成建档（User Persona 生效）；
- 固定 8 轮脚本化话题：日常闲聊 / 分享好消息 / 情绪低落 / 观点分歧 /
  用户短句 / 用户主动结束 / 重新联系 / 人物自己的生活；
- 采集回复后统计「AI 模板化指标」：重复句式、每轮提问率、口头禅滥用、
  人物间口吻趋同度、长度分布；并保留逐条原文供人工看。

用法（后端已在 127.0.0.1:8000 运行）：
    .venv\\Scripts\\python.exe scripts\\eval_persona_chat.py
    .venv\\Scripts\\python.exe scripts\\eval_persona_chat.py --base http://127.0.0.1:8000 --out docs/qa/persona-chat.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent

#: 每个性格一轮的话题脚本（顺序固定，便于跨人物对比）
SCRIPT: list[tuple[str, str]] = [
    ("small_talk", "hey! how's your day going?"),
    ("good_news", "I got a new job today! I'm really excited about it."),
    ("low_mood", "honestly this week has been rough. I feel kind of down."),
    ("disagreement", "I don't think working from home is productive at all."),
    ("short_reply", "yeah."),
    ("user_ends", "anyway I should go. talk later."),
    ("reconnect", "hey, I'm back. what's new with you?"),
    ("persona_life", "what are you up to today?"),
]

#: 目标性格桶：temperament 是 seed/每日世界写进 personas.ai_config 的字段
TARGET_TRAITS = [
    ("outgoing", ("warm", "upbeat", "passionate", "playful")),
    ("reserved", ("quiet", "calm", "sleepy", "poetic")),
    ("witty_sarcastic", ("sarcastic", "witty", "dry", "blunt")),
]

FILLERS = ("yeah", "oh", "honestly", "haha", "i mean", "lol", "well,", "like,")
TEMPLATES = (
    "i'm sorry to hear",
    "sorry to hear that",
    "that sounds",
    "how are you",
    "what about you",
    "good job",
    "well done",
    "let me correct",
    "i hope you",
    "feel free",
)
QUESTION = re.compile(r"[?？]\s*$")
WORD = re.compile(r"[a-zA-Z']+")


def _persona_buckets(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """从本地只读 DB 里挑候选人物（仅测试脚本使用）。

    只保留 **permanent（常驻）** 人物：daily-world 的 guest/adopted 会话过期后
    ``/api/v3/personas/{id}`` 会 404，拿它们做评估会得到假失败。
    性格桶优先按 ``ai_config.temperament``，缺失时按 energy / personality 关键词兜底。
    """
    out: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in TARGET_TRAITS}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, nickname, occupation, city, country, language, age, "
            "personality, interests, ai_config, speech_dna, lifecycle "
            "FROM personas WHERE status='active' AND lifecycle='permanent' "
            "ORDER BY created_at DESC LIMIT 400"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        item = dict(row)
        try:
            cfg = json.loads(item.get("ai_config") or "{}")
        except ValueError:
            cfg = {}
        temperament = str(cfg.get("temperament") or "").lower()
        try:
            energy = float(cfg.get("energy") or 0.5)
        except (TypeError, ValueError):
            energy = 0.5
        for bucket, traits in TARGET_TRAITS:
            hit = temperament in traits
            if not hit and temperament == "":
                text = f"{item.get('personality') or ''} {item.get('nickname') or ''}".lower()
                if bucket == "outgoing" and energy >= 0.65:
                    hit = True
                elif bucket == "reserved" and energy <= 0.45:
                    hit = True
                elif bucket == "witty_sarcastic" and any(
                    k in text for k in ("sarcas", "dry", "witty", "blunt")
                ):
                    hit = True
            if hit:
                out[bucket].append(
                    {
                        "id": item["id"],
                        "nickname": item["nickname"],
                        "temperament": temperament,
                        "energy": energy,
                        "occupation": item.get("occupation"),
                        "city": item.get("city"),
                        "language": item.get("language"),
                        "personality": (item.get("personality") or "")[:220],
                    }
                )
        # 兜底：常驻人物可能是「中间性格」（没命中任何桶），按 energy 归到最近的一档
        if not any(
            item["id"] in [c["id"] for c in out[bucket]] for bucket, _ in TARGET_TRAITS
        ):
            bucket = "outgoing" if energy >= 0.62 else ("reserved" if energy <= 0.42 else "witty_sarcastic")
            out[bucket].append(
                {
                    "id": item["id"],
                    "nickname": item["nickname"],
                    "temperament": temperament or "unlabeled",
                    "energy": energy,
                    "occupation": item.get("occupation"),
                    "city": item.get("city"),
                    "language": item.get("language"),
                    "personality": (item.get("personality") or "")[:220],
                }
            )
    return out


def _register(client: httpx.Client) -> dict[str, str]:
    device = f"eval-{uuid.uuid4().hex}"
    r = client.post("/api/v3/auth/register", json={"device_key": device})
    r.raise_for_status()
    data = r.json()
    ident = {"user_id": str(data["user_id"]), "token": str(data["token"])}
    client.headers.update({"X-User-Id": ident["user_id"], "X-User-Token": ident["token"]})
    return ident


def _complete_profile(client: httpx.Client, nickname: str) -> dict[str, Any]:
    """完成建档（同时验证 Onboarding 后端契约：complete_setup）。"""
    body = {
        "nickname": nickname,
        "native_language": "zh",
        "target_languages": ["en"],
        "levels": {"en": "能聊几句"},
        "interests": ["Programming", "Gaming", "Music"],
        "communication_preference": "natural",
        "complete_setup": True,
    }
    r = client.put("/api/v3/profile", json=body)
    r.raise_for_status()
    return r.json()


def _add_friend(client: httpx.Client, persona_id: str) -> str:
    r = client.post(f"/api/v3/personas/{persona_id}/friend", json={})
    r.raise_for_status()
    return str(r.json().get("friend_id") or "")


def _send(client: httpx.Client, friend_id: str, text: str) -> str:
    cid = f"eval-{uuid.uuid4().hex[:16]}"
    r = client.post(
        f"/api/v2/chat/{friend_id}/text",
        json={"text": text, "client_message_id": cid},
        timeout=180.0,
    )
    r.raise_for_status()
    data = r.json()
    msgs = data.get("friend_messages") or []
    if not msgs:
        return ""
    return str(msgs[0].get("transcript") or "")


def _norm(text: str) -> str:
    return " ".join(WORD.findall(text.lower()))


def _metrics(replies: list[str]) -> dict[str, Any]:
    joined = "\n".join(replies)
    lowers = [r.lower() for r in replies]
    words = [len(WORD.findall(r)) for r in replies]
    questions = sum(1 for r in replies if QUESTION.search(r.strip()))
    dup = 0
    seen: set[str] = set()
    for reply in replies:
        key = _norm(reply)[:40]
        if not key:
            continue
        if key in seen:
            dup += 1
        seen.add(key)
    return {
        "turns": len(replies),
        "chars": sum(len(r) for r in replies),
        "avg_words": round(sum(words) / len(words), 1) if words else 0,
        "min_words": min(words) if words else 0,
        "max_words": max(words) if words else 0,
        "question_turns": questions,
        "question_ratio": round(questions / max(1, len(replies)), 2),
        "duplicate_prefix_turns": dup,
        "language_consistency": _latin_ratio(replies),
        "filler_hits": {f: sum(1 for r in lowers if r.startswith(f)) for f in FILLERS},
        "template_hits": {t: joined.lower().count(t) for t in TEMPLATES if joined.lower().count(t)},
    }


def _similarity(a: list[str], b: list[str], a_name: str, b_name: str) -> float:
    """人物间口吻趋同度：回复 token 集合的 Jaccard（越低越有个性差异）。"""
    sa = {w for r in a for w in WORD.findall(r.lower())}
    sb = {w for r in b for w in WORD.findall(r.lower())}
    if not sa or not sb:
        return 0.0
    return round(len(sa & sb) / len(sa | sb), 3)


def _latin_ratio(replies: list[str]) -> float:
    """英文人物回复里拉丁字母占比（检测「日语人物串英文」这类语言污染）。"""
    text = " ".join(replies)
    if not text:
        return 0.0
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    letters = sum(1 for ch in text if ch.isalpha())
    return round(latin / letters, 3) if letters else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--db", default=str(ROOT / "data" / "speakeasy_v2.db"))
    ap.add_argument("--out", default="")
    ap.add_argument("--nickname", default="Alex")
    args = ap.parse_args()

    buckets = _persona_buckets(Path(args.db))
    all_candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for bucket, _traits in TARGET_TRAITS:
        for item in buckets[bucket]:
            if (item.get("language") or "en") != "en":
                continue
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            all_candidates.append(item)
    # 常驻英文人物很少（种子世界 5 个），用「能量/气质」挑出三种差异最大的：
    # 表达欲强（energy 最高）、安静（energy 最低）、毒舌/幽默（dry/witty/sarcastic）。
    def _api_ok(client: httpx.Client, persona_id: str) -> bool:
        try:
            return client.get(f"/api/v3/personas/{persona_id}").status_code == 200
        except Exception:  # noqa: BLE001
            return False

    picked: list[tuple[str, dict[str, Any]]] = []
    used: set[str] = set()
    with httpx.Client(base_url=args.base, timeout=20.0) as probe_client:
        valid = [p for p in all_candidates if _api_ok(probe_client, p["id"])]
        if len(valid) < 3:
            print(f"只找到 {len(valid)} 个可用英文常驻人物，无法完成 3 种性格评估", file=sys.stderr)
            return 2
        by_energy = sorted(valid, key=lambda p: -float(p.get("energy") or 0))
        expressive = by_energy[0]
        picked.append(("outgoing", expressive))
        used.add(expressive["id"])
        remaining = [p for p in valid if p["id"] not in used]
        reserved = min(remaining, key=lambda p: float(p.get("energy") or 0))
        picked.append(("reserved", reserved))
        used.add(reserved["id"])
        remaining = [p for p in remaining if p["id"] not in used]

        def _dry_score(persona: dict[str, Any]) -> int:
            text = f"{persona.get('temperament')} {persona.get('personality')}".lower()
            return sum(2 for key in ("sarcas", "witty", "dry", "blunt") if key in text)

        witty = max(remaining, key=_dry_score)
        picked.append(("witty_sarcastic", witty))

    report: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "base": args.base,
        "profile": {
            "nickname": args.nickname,
            "native_language": "zh",
            "target_languages": ["en"],
            "level": "能聊几句",
            "interests": ["Programming", "Gaming", "Music"],
            "communication_preference": "natural",
        },
        "personas": {},
    }

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        for bucket, persona in picked:
            label = f"{bucket}:{persona['nickname']}"
            print(f"\n== {label} ({persona['id']}) temperament={persona['temperament']} energy={persona['energy']}")
            try:
                ident = _register(client)
                _complete_profile(client, f"Alex-{bucket}")
                friend_id = _add_friend(client, persona["id"])
                transcript: list[dict[str, Any]] = []
                replies: list[str] = []
                for topic, text in SCRIPT:
                    started = time.time()
                    reply = _send(client, friend_id, text)
                    cost = round(time.time() - started, 2)
                    transcript.append({"topic": topic, "user": text, "persona": reply, "seconds": cost})
                    replies.append(reply)
                    print(f"  [{topic}] {cost:>5.1f}s  user: {text}")
                    print(f"                 persona: {reply}")
                report["personas"][label] = {
                    "bucket": bucket,
                    "persona": persona,
                    "identity": ident["user_id"],
                    "friend_id": friend_id,
                    "transcript": transcript,
                    "metrics": _metrics(replies),
                }
            except Exception as exc:  # noqa: BLE001 - 单个失败不影响其它
                report["personas"][label] = {"bucket": bucket, "persona": persona, "error": f"{type(exc).__name__}: {exc}"}
                print(f"  [ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)

    # 人物间趋同度
    keys = [k for k, v in report["personas"].items() if "transcript" in v]
    sim: dict[str, float] = {}
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a = [t["persona"] for t in report["personas"][keys[i]]["transcript"]]
            b = [t["persona"] for t in report["personas"][keys[j]]["transcript"]]
            sim[f"{keys[i]} vs {keys[j]}"] = _similarity(a, b, keys[i], keys[j])
    report["cross_persona_similarity"] = sim
    report["summary"] = {
        key: {
            "avg_words": val["metrics"]["avg_words"],
            "question_ratio": val["metrics"]["question_ratio"],
            "duplicate_prefix_turns": val["metrics"]["duplicate_prefix_turns"],
            "filler_starts": sum(val["metrics"]["filler_hits"].values()),
            "template_hits": sum(val["metrics"]["template_hits"].values()),
            "language_consistency": val["metrics"]["language_consistency"],
        }
        for key, val in report["personas"].items()
        if "metrics" in val
    }

    # 自动验收：这些是「AI 模板化」的可量化代理指标（人工再复核原文）
    verdict = {
        "no_template_phrases": all(
            sum(v["metrics"]["template_hits"].values()) == 0 for v in report["personas"].values() if "metrics" in v
        ),
        "no_duplicate_openings": all(
            v["metrics"]["duplicate_prefix_turns"] <= 1 for v in report["personas"].values() if "metrics" in v
        ),
        "not_question_every_turn": all(
            v["metrics"]["question_ratio"] <= 0.6 for v in report["personas"].values() if "metrics" in v
        ),
        "distinct_voices": all(score < 0.35 for score in sim.values()),
        "language_consistent": all(
            float(v["metrics"]["language_consistency"]) >= 0.85
            for v in report["personas"].values()
            if "metrics" in v
        ),
        "all_personas_ran": len(report["personas"]) == 3
        and all("transcript" in v for v in report["personas"].values()),
    }
    report["verdict"] = verdict
    report["passed"] = all(verdict.values())

    out = Path(args.out) if args.out else ROOT / "docs" / "qa" / f"persona-chat-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print("cross_persona_similarity:", json.dumps(sim, ensure_ascii=False))
    print("verdict:", json.dumps(verdict, ensure_ascii=False, indent=2))
    print("PASSED:", report["passed"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
