"""Lead 自测：v2 数据层 / 时区 / 润色规则 / 音频存储（全部离线）。"""

from __future__ import annotations

import io
import math
import os
import struct
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.audio_store import AudioStore  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v2.suggestion import generate_suggestion, rule_suggestion  # noqa: E402
from app.v2.timeutil import (  # noqa: E402
    format_for_prompt,
    local_date_str,
    local_state,
    moment_local_label,
)


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "speakeasy_v2.db"))
    database.init()
    yield database
    database.close()


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def test_profile_default_and_update(db):
    profile = db.get_profile()
    assert profile["nickname"] == "Traveler"
    assert profile["fact_sheet"]["profile"]["nickname"] == "Traveler"

    updated = db.update_profile(
        nickname="Yulin", fact_sheet={"interests": ["indie games"]}
    )
    assert updated["nickname"] == "Yulin"
    assert updated["fact_sheet"]["interests"] == ["indie games"]

    # 再次读取仍是新值
    assert db.get_profile()["fact_sheet"]["interests"] == ["indie games"]


def test_friend_crud_and_json_columns(db):
    friend = db.create_friend(
        {
            "name": "Marco",
            "nationality": "Italy",
            "city": "Rome",
            "timezone": "Europe/Rome",
            "native_lang": "it",
            "archetype_id": "it_multilingual_male_01",
            "voice_id": "it-IT-GiuseppeMultilingualNeural",
            "speech_quirks": ["allora", "dai"],
            "personality_prompt": "warm roman designer",
        }
    )
    assert friend["speech_quirks"] == ["allora", "dai"]
    assert friend["status"] == "active"
    assert friend["closeness_level"] == 1
    assert friend["id"] in {f["id"] for f in db.list_friends()}
    assert db.used_archetype_ids() == {"it_multilingual_male_01"}

    friend = db.update_friend(
        friend["id"],
        status="pending_request",
        shared_episodes=["2026-11-03: talked about vinyl"],
        open_loops=[{"event": "interview", "follow_up_after": "2026-11-05T18:00:00+00:00",
                     "status": "pending"}],
    )
    assert friend["status"] == "pending_request"
    assert friend["shared_episodes"][0].startswith("2026-11-03")
    assert friend["open_loops"][0]["event"] == "interview"
    assert db.list_friends("pending_request")[0]["id"] == friend["id"]
    assert db.list_friends("active") == []

    friend = db.update_friend(friend["id"], status="active", closeness_level=2)
    assert friend["closeness_level"] == 2


def test_reply_language_default_and_update(db):
    friend = db.create_friend({"name": "Yui", "timezone": "Asia/Tokyo", "native_lang": "ja"})
    assert friend["reply_language"] == "en"  # 默认英语；翻译由 spawner 决定
    friend = db.update_friend(friend["id"], reply_language="ja")
    assert friend["reply_language"] == "ja"
    assert db.get_friend(friend["id"])["reply_language"] == "ja"


def test_friend_ordering_by_last_interaction(db):
    a = db.create_friend({"name": "A", "timezone": "UTC"})
    b = db.create_friend({"name": "B", "timezone": "UTC"})
    db.touch_friend(a["id"])
    names = [f["name"] for f in db.list_friends()]
    assert names[0] == "A"
    db.delete_friend(a["id"])
    assert names and [f["name"] for f in db.list_friends()] == [b["name"]]


def test_messages_turns_unread_and_pagination(db):
    friend = db.create_friend({"name": "Emily", "timezone": "America/New_York"})
    fid = friend["id"]
    base = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        db.add_message(
            fid,
            "user" if i % 2 == 0 else "friend",
            f"msg-{i}",
            created_at=(base + timedelta(minutes=i)).isoformat(),
            is_read=(i % 2 == 0),
        )
    assert db.count_turns(fid) == 3
    assert db.count_messages(fid) == 5
    assert db.unread_count(fid) == 2
    assert db.last_message(fid)["transcript"] == "msg-4"

    rows = db.list_messages(fid, limit=2)
    assert [m["transcript"] for m in rows] == ["msg-3", "msg-4"]

    before = rows[0]["created_at"]
    older = db.list_messages(fid, limit=10, before=before)
    assert [m["transcript"] for m in older] == ["msg-0", "msg-1", "msg-2"]

    db.mark_read(fid)
    assert db.unread_count(fid) == 0

    msg = db.get_message(rows[-1]["id"])
    updated = db.update_message(
        msg["id"],
        suggestion={"better_version": "x", "explanation": "y", "tag": "chunk", "is_saved": False},
    )
    assert updated["suggestion"]["better_version"] == "x"


def test_expressions_crud_and_pick(db):
    e1 = db.add_expression("I very like it", "I'm really into it", "why", None)
    e2 = db.add_expression("open the light", "turn on the light", "why2", None)
    assert len(db.list_expressions()) == 2
    assert db.pick_review_expressions(1)[0]["id"] == e1["id"]

    db.update_expression(e1["id"], mastery_count=3, status="mastered")
    assert db.pick_review_expressions(5)[0]["id"] == e2["id"]
    db.delete_expression(e2["id"])
    assert len(db.list_expressions()) == 1


def test_moments_idempotency(db):
    friend = db.create_friend({"name": "Aria", "timezone": "Europe/Rome"})
    assert not db.moments_exist_for_date("2026-11-03")
    m = db.add_moment(friend["id"], "morning espresso", "☕", "8:00 AM · Rome", "2026-11-03")
    assert db.moments_exist_for_date("2026-11-03")
    assert db.list_moments()[0]["content"] == "morning espresso"
    db.mark_moment_replied(m["id"])
    assert db.get_moment(m["id"])["is_replied"] == 1


# --------------------------------------------------------------------------- #
# 时区
# --------------------------------------------------------------------------- #
def test_local_state_periods_and_labels():
    night = datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc)
    ny = local_state("America/New_York", night)
    assert ny["local_time"] == "17:30"
    assert ny["period"] == "evening"
    assert ny["is_awake"] is True
    assert "New_York" not in ny["activity"]
    prompt = format_for_prompt(ny)
    assert "Your local time" in prompt and "weekday" not in prompt.split("(")[0]

    sleeping = local_state("UTC", datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc))
    assert sleeping["period"] == "sleeping"
    assert sleeping["is_awake"] is False
    assert local_date_str("UTC", datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)) == "2026-11-03"

    label = moment_local_label("Europe/Rome", "Rome", datetime(2026, 11, 3, 7, 45, tzinfo=timezone.utc))
    assert label == "8:45 AM · Rome"

    # 非法时区回退 UTC
    fallback = local_state("Not/AZone", datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))
    assert fallback["local_time"] == "12:00"


# --------------------------------------------------------------------------- #
# 润色规则
# --------------------------------------------------------------------------- #
def test_rule_suggestion_hits_and_misses():
    hit = rule_suggestion("I very like this coffee, it is too much good")
    assert hit is not None
    assert "really" in hit["better_version"].lower()
    assert hit["tag"] in {"grammar", "chunk"}

    assert rule_suggestion("How to say this in English?") is not None
    assert rule_suggestion("I am agree with you") is not None
    assert rule_suggestion("This coffee is amazing") is None


@pytest.mark.asyncio
async def test_generate_suggestion_non_english_offline_returns_none():
    class MockLLM:
        uses_mock = True

    from app.v2.suggestion import generate_suggestion

    assert await generate_suggestion(MockLLM(), "これはいいですね", language="ja") is None


@pytest.mark.asyncio
async def test_generate_suggestion_offline_fallback():
    class MockLLM:
        uses_mock = True

    result = await generate_suggestion(MockLLM(), "I very like this coffee")
    assert result is not None
    assert result["better_version"]
    assert result["is_saved"] is False

    assert await generate_suggestion(MockLLM(), "nice") is None


# --------------------------------------------------------------------------- #
# 音频存储
# --------------------------------------------------------------------------- #
def _make_wav_bytes(seconds: float = 0.5, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for i in range(int(seconds * sr)):
            value = int(0.25 * 32767 * math.sin(2 * math.pi * 440 * i / sr))
            frames += struct.pack("<h", value)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def test_audio_store_decode_and_serve(tmp_path):
    store = AudioStore(str(tmp_path))
    assert os.path.isdir(store.audio_dir)

    decoded = store.decode_upload(_make_wav_bytes(0.5), "voice.wav")
    assert decoded["sample_rate"] == 16000
    assert 400 <= decoded["duration_ms"] <= 700
    assert len(decoded["pcm16"]) > 0
    assert os.path.isfile(store.path_for(decoded["audio_filename"]))
    assert store.url_for(decoded["audio_filename"]).startswith("/api/v2/audio/")

    with pytest.raises(ValueError):
        store.decode_upload(b"", "empty.wav")
    safe = store.path_for("../evil.wav")
    assert os.path.dirname(safe) == store.audio_dir
