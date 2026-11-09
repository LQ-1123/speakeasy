"""第四阶段验收：一个角色 = 一个声音（聊天 / 动态朗读 / 通话 / 试听一致）。

背景（用户验收反馈）
-------------------
「消息中的语音和用户动态中的语音不一样」——根因是同一个人物在不同链路各自解析音色：
聊天读 ``friends.fish_voice_id``（建好友时的快照），朗读读 ``personas.fish_voice_id``，
Fish 失败后朗读还把 Fish 音色 id 当 Edge 声线用（必然失败→tone），于是同一个人两个声音。

本文件锁定四件事：
1. 同一人物，聊天/朗读/通话/试听解析出**同一个** VoiceBinding；
2. Fish 不可用（本机真实情况）时，四者统统降级到**同一个** Edge 声线，且该声线合法；
3. 朗读缓存文件名带 provider token：Fish 恢复后不会继续播降级音频；
4. 朗读不再按帖子语言换声线（否则跨语言动态会变成另一个声音）。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.v2.db import DB
from app.v3 import audio_cache
from app.v3.schema import ensure_schema
from app.voice import voice_registry
from app.voice.voice_registry import (
    binding_for_friend,
    binding_for_persona,
    edge_voice_valid,
)

CFG_FISH = SimpleNamespace(
    provider="fish",
    fish_api_key="test-key",
    fish_base_url="https://api.fish.audio",
    fish_model="s2.1-pro",
    fish_reference_id="global-ref",
    fish_format="mp3",
)
CFG_EDGE = SimpleNamespace(provider="edge", fish_api_key="", fish_reference_id="")


class _FakeEdge:
    """记录被要求合成的 Edge 声线。"""

    def __init__(self) -> None:
        self.voices: list[str] = []

    async def synth_to_wav(self, text, voice, rate="+0%", pitch="+0Hz", out_dir=None):
        self.voices.append(voice)
        target_dir = out_dir or os.getcwd()
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, f"fake_{len(self.voices)}.wav")
        with open(path, "wb") as fh:
            fh.write(b"RIFF" + b"\x00" * 2048)
        return path, 800


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "voice.db"))
    database.init()
    ensure_schema(database, seed=True)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _fish_enabled_for_tests(monkeypatch):
    """本文件专门验证 Fish/Edge 切换：显式打开 Fish 并标记"已验证可用"。

    生产语义：只有真的探测/合成成功过才用 Fish（避免「朗读命中旧 Fish 缓存、
    聊天新合成失败走 Edge」的双声），所以测试要先模拟一次成功。
    """
    monkeypatch.setenv("SE_TTS_FISH", "1")
    monkeypatch.setattr(voice_registry, "load_cfg", lambda store=None: CFG_FISH)
    voice_registry.reset_state()
    voice_registry.mark_fish_success()


@pytest.fixture(autouse=True)
def _reset_breaker():
    voice_registry.reset_state()
    # 生产语义：Fish 需要"探测成功过"才可用；本文件默认模拟 Fish 正常
    if str(os.getenv("SE_TTS_FISH", "0")).strip().lower() in ("1", "true", "yes", "on"):
        voice_registry.mark_fish_success()
    yield
    voice_registry.reset_state()


def _persona(db: DB, nickname: str) -> dict:
    row = db.conn.execute("SELECT * FROM personas WHERE nickname=?", (nickname,)).fetchone()
    assert row is not None, nickname
    return dict(row)


def _with_fish_voice(db: DB, persona: dict, voice: str = "fish-voice-aaa") -> dict:
    """测试库里的种子人物没有 Fish 音色（分配要联网），这里手动指定。"""
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute("UPDATE personas SET fish_voice_id=? WHERE id=?", (voice, persona["id"]))
    return _persona(db, persona["nickname"])


def _make_friend(db: DB, persona: dict, *, fish_voice_id: str = "") -> dict:
    return db.create_friend(
        {
            "name": persona["nickname"],
            "persona_id": persona["id"],
            "voice_id": persona.get("voice_id") or "",
            "fish_voice_id": fish_voice_id,
            "native_lang": persona.get("language") or "en",
            "reply_language": persona.get("language") or "en",
            "personality_prompt": "x",
        }
    )


# --------------------------------------------------------------------------- #
# 1. 四链路同一个 binding
# --------------------------------------------------------------------------- #
def test_chat_and_speech_and_call_share_one_binding(db):
    persona = _with_fish_voice(db, _persona(db, "Adrian"))
    friend = _make_friend(db, persona, fish_voice_id=persona["fish_voice_id"])

    chat_binding = binding_for_friend(db, friend, cfg=CFG_FISH)
    speech_binding = binding_for_persona(persona, CFG_FISH)
    # 通话走的是同一份好友行，因此同样得到 chat_binding
    assert chat_binding.token == speech_binding.token
    assert chat_binding.provider == speech_binding.provider == "fish"
    assert chat_binding.ref == persona["fish_voice_id"]
    assert chat_binding.edge_voice == speech_binding.edge_voice


def test_stale_friend_row_falls_back_to_live_persona_voice(db):
    """好友行里的 fish 音色是旧快照时，聊天必须用 persona 的当前音色。"""
    persona = _with_fish_voice(db, _persona(db, "Camila"), "fish-voice-new")
    friend = _make_friend(db, persona, fish_voice_id="stale-old-voice")

    chat_binding = binding_for_friend(db, friend, cfg=CFG_FISH)
    speech_binding = binding_for_persona(persona, CFG_FISH)
    assert chat_binding.token == speech_binding.token
    assert chat_binding.ref == persona["fish_voice_id"] != "stale-old-voice"


# --------------------------------------------------------------------------- #
# 2. Fish 不可用 → 全员同一个合法 Edge 声线
# --------------------------------------------------------------------------- #
def test_fish_down_degrades_to_same_edge_voice_everywhere(db):
    persona = _persona(db, "Haruto")
    friend = _make_friend(db, persona)

    # 模拟连接失败：熔断后 fish_usable 返回 False
    voice_registry.mark_fish_failure(RuntimeError("All connection attempts failed"))

    chat_binding = binding_for_friend(db, friend, cfg=CFG_FISH)
    speech_binding = binding_for_persona(persona, CFG_FISH)
    assert chat_binding.provider == speech_binding.provider == "edge"
    assert chat_binding.token == speech_binding.token
    assert chat_binding.edge_voice == speech_binding.edge_voice
    assert edge_voice_valid(chat_binding.token.removeprefix("edge-"))


def test_edge_voice_is_always_legal_even_for_fish_token_ids(db):
    """回归：之前把 `fish-xxxxxxxx` 当 Edge 声线用，必然失败→tone。"""
    persona = _with_fish_voice(db, _persona(db, "Lin"))
    broken = dict(persona, voice_id="fish-deadbeef")  # 人为弄脏
    binding = binding_for_persona(broken, CFG_EDGE)
    assert binding.provider == "edge"
    assert edge_voice_valid(binding.edge_voice)
    assert not binding.edge_voice.startswith("fish-")


# --------------------------------------------------------------------------- #
# 3. 朗读缓存：带 provider token + 用角色自己的声线
# --------------------------------------------------------------------------- #
def _make_cache(tmp_path, edge_engine=None) -> audio_cache.PersonaAudioCache:
    return audio_cache.PersonaAudioCache(None, edge_engine or _FakeEdge(), str(tmp_path / "audio"))


def test_speech_cache_filename_carries_provider_token(tmp_path, db):
    persona = _with_fish_voice(db, _persona(db, "Adrian"))
    post = {"id": "pp_x", "content": "Spent the morning arguing with a comma.", "language": "en"}
    cache = _make_cache(tmp_path)

    fish_path, _lang, fish_token = cache.path_for(post, persona)
    assert "-fish-" in os.path.basename(fish_path)
    assert fish_token.startswith("fish-")  # token 是稳定身份

    voice_registry.mark_fish_failure(RuntimeError("boom"))
    edge_path, _lang2, edge_token = cache.path_for(post, persona)
    # 身份不变（聊天/朗读永远对比得到同一个 token），但渲染后端变了 → 文件不同
    assert edge_token == fish_token
    assert "-edge-" in os.path.basename(edge_path)
    assert edge_path != fish_path
    # Fish 恢复后回到 Fish 版本（文件名再次不同），不会一直播降级音频
    voice_registry.reset_state()
    voice_registry.mark_fish_success()  # 模拟探测成功 = Fish 恢复
    fish_path2, _l3, fish_token2 = cache.path_for(post, persona)
    assert fish_token2 == fish_token
    assert fish_path2 == fish_path != edge_path


def test_speech_synthesis_uses_persona_voice_not_post_language(tmp_path, db):
    """跨语言帖子也必须用角色自己的声音（Fish 多语言），否则同一个人会变声。"""
    persona = _with_fish_voice(db, _persona(db, "Lin"))  # en
    post = {"id": "pp_mix", "content": "Guten Morgen, ich war beim Bäcker.", "language": "de"}
    edge = _FakeEdge()
    cache = _make_cache(tmp_path, edge)
    voice_registry.mark_fish_failure(RuntimeError("down"))  # 测试环境没有 Fish

    path, _lang, token = asyncio.run(cache.get_or_synth(post, persona))
    assert os.path.exists(path)
    # 身份稳定（该角色固定的 Fish 音色），但本次是降级渲染（文件名带 edge-）
    assert token.startswith("fish-")
    assert "-edge-" in os.path.basename(path)
    assert edge.voices == [persona["voice_id"]]  # 用的是她自己的声线


def test_speech_synthesis_records_edge_provider_stats(tmp_path, db):
    persona = _persona(db, "Adrian")
    post = {"id": "pp_stat", "content": "Short note.", "language": "en"}
    cache = _make_cache(tmp_path)
    asyncio.run(cache.get_or_synth(post, persona))
    stats = cache.stats()
    assert stats["ready"] >= 1
    assert stats["edge_files"] >= 1
    assert "fish" in stats and "reachable" in stats["fish"]


# --------------------------------------------------------------------------- #
# 4. 熔断：Fish 失败后不再每次等超时；成功后恢复
# --------------------------------------------------------------------------- #
def test_breaker_marks_fish_down_then_recovers():
    assert voice_registry.fish_usable(CFG_FISH) is True
    voice_registry.mark_fish_failure(RuntimeError("connect timeout"))
    assert voice_registry.fish_usable(CFG_FISH) is False
    status = voice_registry.fish_status()
    assert status["reachable"] is False
    assert "connect timeout" in status["last_error"]
    voice_registry.mark_fish_success()
    assert voice_registry.fish_usable(CFG_FISH) is True


def test_synthesize_falls_back_to_edge_and_keeps_token(tmp_path, db):
    persona = _persona(db, "Camila")
    edge = _FakeEdge()
    voice_registry.mark_fish_failure(RuntimeError("down"))
    result = asyncio.run(
        voice_registry.synthesize(
            persona,
            "Oi gente, tudo bem?",
            binding=binding_for_persona(persona, CFG_FISH),
            cfg=CFG_FISH,
            edge_engine=edge,
            out_dir=str(tmp_path),
        )
    )
    assert result is not None
    assert result.provider == "edge"
    assert result.ext == "wav"
    assert edge.voices == [persona["voice_id"]]
    assert result.token.startswith("edge-")


def test_fish_usable_unknown_and_retry_after_cooldown():
    """重启后 attempt_ok=None 不能永远走 Edge；失败后冷却结束要允许重试。"""
    import time as _time

    voice_registry.reset_state()
    # 刚启动（未知状态）：允许试一次 Fish
    assert voice_registry.fish_usable(CFG_FISH) is True

    voice_registry.mark_fish_failure(RuntimeError("boom"))
    # 熔断窗口内：走 Edge
    assert voice_registry.fish_usable(CFG_FISH) is False
    # 冷却结束：允许再次尝试（否则失败了就永远回不来）
    retry_at = _time.time() + voice_registry.FISH_DOWN_SECONDS + 1
    assert voice_registry.fish_usable(CFG_FISH, now=retry_at) is True

    voice_registry.mark_fish_success()
    assert voice_registry.fish_usable(CFG_FISH) is True
    assert voice_registry.fish_status()["attempt_ok"] is True

    # 非 fish 配置永远不可用
    assert voice_registry.fish_usable(CFG_EDGE) is False
