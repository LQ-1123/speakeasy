"""Sample the actual chat and post generators in an isolated temporary world.

Uses the existing model configuration; never reads user conversations or writes to
the app database. Run with --live to make bounded model requests. Output includes
raw replies, prompts, call failures and mechanical metrics, not a realism score.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.runtime_config import LLMConfigStore, apply_to_settings
from app.v2.chat_service import ChatService, split_emotion
from app.v2.db import DB
from app.v2.memory import MemoryManager
from app.v3 import event_posts, life_events, life_posts, prompt_composer, speech_dna
from app.v3.schema import ensure_schema
from app.v3.service import V3Service

OPEN_DATABASES = []


class ObservedModel:
    uses_mock = False

    def __init__(self, client):
        self.client = client
        self.calls = []

    async def stream_chat(self, messages, **kwargs):
        record = {"kind": "chat", "messages": messages, "settings": kwargs, "output": ""}
        self.calls.append(record)
        try:
            async for delta in self.client.stream_chat(messages, **kwargs):
                record["output"] += delta
                yield delta
        except Exception as exc:
            record["error"] = type(exc).__name__
            raise

    async def complete_json(self, messages, **kwargs):
        record = {"kind": "json", "messages": messages, "settings": kwargs}
        self.calls.append(record)
        try:
            result = await self.client.complete_json(messages, **kwargs)
            record["output"] = result
            return result
        except Exception as exc:
            record["error"] = type(exc).__name__
            raise


EN = [
    "hey, how's it going?",
    "I got the job! They called me this morning.",
    "But my cat is ill. I'm waiting for the vet to call, can't really focus.",
    "Please don't give me advice, just stay here a bit.",
    "yeah",
    "What happened with the book you were talking about?",
    "You were at a concert in Paris last night, right?",
    "I need to go now. talk later",
]
ZH = [
    "在干嘛呢",
    "我拿到offer了，早上刚打的电话",
    "但我家猫生病了，还在等医生电话，有点烦",
    "不用给我建议，陪我待会儿就行",
    "嗯",
    "你之前说的那本书后来呢",
    "你昨晚不是在巴黎看演唱会吗",
    "我先走了，下次聊",
]


async def sample(client, directory, persona_id, *, chinese=False):
    db = DB(str(directory / (persona_id + ".db")))
    OPEN_DATABASES.append(db)
    db.init()
    ensure_schema(db, seed=True)
    await speech_dna.ensure_persona_dna(db, None, use_llm=False)
    if chinese:
        with db._lock, db.conn:
            db.conn.execute(
                "UPDATE personas SET nickname=?,language='zh',city=?,country='China',"
                "occupation=?,bio=?,personality=?,speech_dna='{}',timezone='Asia/Shanghai',"
                "interests='[\"drawing\",\"cats\",\"books\"]' WHERE id=?",
                ("小林", "杭州", "插画师", "画画。养猫。书经常看一半。",
                 "话不多，熟了会吐槽，喜欢具体地聊事情。不把每句话变成笑话。", persona_id),
            )
            # A translated test identity must not inherit a different person's public history.
            db.conn.execute("DELETE FROM persona_posts WHERE persona_id=?", (persona_id,))
    persona = dict(db.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone())
    friend_data = {**prompt_composer.friend_for_persona(persona, persona_id), "total_turns": 8}
    columns = {r[1] for r in db.conn.execute("PRAGMA table_info(friends)")}
    friend = db.create_friend({k: v for k, v in friend_data.items() if k in columns})
    model = ObservedModel(client)
    engines = SimpleNamespace(llm=model)
    chat = ChatService(engines, db, MemoryManager(db, model), None, None, None)
    service = V3Service(db, engines, str(directory))
    now = datetime.now(timezone.utc)
    event = {
        "persona_id": persona_id, "participants": [persona_id],
        "event_type": "entertainment", "title": "Stopped reading a book",
        "description": "Gave up halfway through a book because the characters were uninteresting. Kept the bookmark.",
        "start_time": (now - timedelta(hours=2)).isoformat(),
        "end_time": (now - timedelta(hours=1)).isoformat(),
        "event_status": "completed", "visibility": "public", "location": "",
        "dedupe_key": persona_id + ":eval-book", "day": now.date().isoformat(),
    }
    event["id"], _ = life_events.add_event(db, event)
    history = []
    turns = []
    for text in (ZH if chinese else EN):
        first = len(model.calls)
        reply = await chat._llm_reply(friend, text, history, [])
        raw_reply = reply
        reply, _speech = split_emotion(reply)
        calls = model.calls[first:]
        turns.append({"user": text, "reply": reply, "raw_reply": raw_reply,
                      "model_calls": len(calls), "errors": [c["error"] for c in calls if c.get("error")]})
        db.add_message(friend["id"], "user", text, msg_type="text")
        db.add_message(friend["id"], "friend", reply, msg_type="text")
        history.extend([{"sender": "user", "transcript": text}, {"sender": "friend", "transcript": reply}])
    post = await event_posts.generate_post_for_participant(db, model, event, persona)
    image = await life_posts.image_caption(
        model, persona, title="A used book with handwritten notes", tags=["book", "annotations"],
        recent=[post["content"]] if post else [],
    )
    comment = await service._llm_comment(persona, "My cat is ill. Waiting for the vet to call.")
    words = [len(re.findall(r"\w+", row["reply"])) for row in turns]
    result = {
        "persona": {k: persona.get(k) for k in ("nickname", "language", "occupation", "personality")},
        "fixture": "synthetic character" if chinese else "repository seed character",
        "known_event": event, "turns": turns, "event_post": post,
        "image_caption_candidate": image, "public_comment": comment,
        "metrics": {"question_mark_turns": sum("?" in t["reply"] or "？" in t["reply"] for t in turns),
                    "min_chars": min(len(t["reply"]) for t in turns),
                    "max_chars": max(len(t["reply"]) for t in turns),
                    "min_words": min(words), "max_words": max(words),
                    "model_calls": len(model.calls), "failed_calls": sum(bool(c.get("error")) for c in model.calls)},
        "calls": model.calls,
    }
    db.close()
    print(f"sample_complete {persona_id}: calls={len(model.calls)} errors={result['metrics']['failed_calls']}", flush=True)
    return result


async def main(args):
    logging.disable(logging.CRITICAL)
    path = ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    # Check report access before paying for model calls.
    path.write_text('{"status":"running"}', encoding="utf-8")
    settings = get_settings()
    config = settings.llm.model_copy(deep=True)
    saved = LLMConfigStore(str(ROOT / "data"), env_defaults=config.model_dump()).config
    apply_to_settings(config, saved)
    config.timeout_s = 35
    config.max_retries = 0
    client = LLMClient(config)
    if client.uses_mock or not client.configured:
        raise SystemExit("A configured real model is required; refusing mock evaluation.")
    tmp_root = ROOT / ".tmp"
    tmp_root.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="realism-", dir=tmp_root) as temp:
            try:
                samples = await asyncio.gather(
                    sample(client, Path(temp), "p_adrian"),
                    sample(client, Path(temp), "p_mei"),
                    sample(client, Path(temp), "p_annika", chinese=True),
                    return_exceptions=True,
                )
                for result in samples:
                    if isinstance(result, BaseException):
                        raise result
            finally:
                for db in OPEN_DATABASES:
                    db.close()
                OPEN_DATABASES.clear()
    finally:
        await client.aclose()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "model": config.model,
              "prompt_version": prompt_composer.PROMPT_VERSION,
              "scope": "Actual generators; isolated synthetic data; no production writes; not an A/B comparison.",
              "samples": samples}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# 人物真实感：模型原始采样", "",
          f"模型：{config.model}；提示词：{prompt_composer.PROMPT_VERSION}。",
          "隔离测试库；真实生成函数；中文角色为测试虚构人物。未写入现有聊天或动态。",
          "这是单次抽样，不是盲测或稳定质量评分；错误与完整输入见同名 JSON。", ""]
    for item in samples:
        md.extend([f"## {item['persona']['nickname']}", "", str(item["metrics"]), ""])
        for turn in item["turns"]:
            md.extend([f"用户：{turn['user']}", "", f"回复：{turn['reply']}", ""])
        md.extend(["事件动态：", "", (item["event_post"] or {}).get("content") or "（跳过）", "",
                   "图库分享候选：", "", str(item["image_caption_candidate"].get("content") or "（跳过）"), "",
                   "公开评论：", "", item["public_comment"] or "（跳过）", ""])
    path.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
    print(f"report_written {path.name}", flush=True)
    if any(item["metrics"]["failed_calls"] for item in samples):
        raise SystemExit("Sampling contained model failures; do not treat fallback output as model quality.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Generate three isolated conversations and posts with the configured model")
    parser.add_argument("--out", default=".tmp/persona-realism-samples.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to make real model requests.")
    asyncio.run(main(args))
