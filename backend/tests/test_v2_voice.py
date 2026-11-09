"""SpeakEasy 2.0 · 声线原型底池 + FriendVoiceSynth 离线单测。

**全程不联网**：edge 合成被 monkeypatch 成假的 numpy 波形；tone 兜底是纯 numpy。
覆盖：
- 28 个原型的数量/必填字段/声线名格式/时区/城市池/名字池/职业池；
- 14 个国家覆盖（美/英/澳/印/爱尔兰/德/意/西/法/中 + 日/韩/巴/墨）；
- v2.1 新增的 ja/ko/pt/es-MX 声线（真实 edge voice_id）与日韩口头禅；
- FriendVoiceSynth：edge 成功写 24k wav + duration_ms、rate/pitch 透传、
  进程内缓存、edge 失败降级 tone。
"""

from __future__ import annotations

import os
import re
import wave
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.v2.voices import (
    ARCHETYPES_PATH,
    FriendVoiceSynth,
    archetype_by_id,
    load_archetypes,
    pitch_to_hz,
    rate_to_speed,
)
from app.voice.edge_tts_engine import EdgeTTSEngine

SR = 24000

_REQUIRED_ARCHETYPE_FIELDS = {
    "archetype_id",
    "voice_id",
    "prosody",
    "locale_info",
    "vibe_constraints",
}
_REQUIRED_COUNTRIES = {
    "United States",
    "United Kingdom",
    "Australia",
    "India",
    "Ireland",
    "Germany",
    "Italy",
    "Spain",
    "France",
    "China",
    # v2.1 多语言好友（Japan/Korea/Brazil/Mexico）
    "Japan",
    "Korea",
    "Brazil",
    "Mexico",
}
#: 新增的 8 个多语言原型：archetype_id → (voice_id, country, native_lang, timezone)
_MULTILINGUAL_ARCHETYPES: dict[str, tuple[str, str, str, str]] = {
    "jp_male_01": ("ja-JP-KeitaNeural", "Japan", "ja", "Asia/Tokyo"),
    "jp_female_01": ("ja-JP-NanamiNeural", "Japan", "ja", "Asia/Tokyo"),
    "kr_male_01": ("ko-KR-InJoonNeural", "Korea", "ko", "Asia/Seoul"),
    "kr_female_01": ("ko-KR-SunHiNeural", "Korea", "ko", "Asia/Seoul"),
    "br_male_01": ("pt-BR-AntonioNeural", "Brazil", "pt", "America/Sao_Paulo"),
    "br_female_01": ("pt-BR-ThalitaMultilingualNeural", "Brazil", "pt", "America/Sao_Paulo"),
    "mx_male_01": ("es-MX-JorgeNeural", "Mexico", "es", "America/Mexico_City"),
    "mx_female_01": ("es-MX-DaliaNeural", "Mexico", "es", "America/Mexico_City"),
}
_GENERIC_OCCUPATIONS = {"worker", "employee", "professional", "person", "student", "staff"}


# --------------------------------------------------------------------------- #
# 原型底池
# --------------------------------------------------------------------------- #
def test_archetypes_file_exists() -> None:
    assert os.path.isfile(ARCHETYPES_PATH), f"缺少声线原型底池: {ARCHETYPES_PATH}"


def test_archetype_pool_has_twenty_eight_unique_prototypes() -> None:
    archetypes = load_archetypes()
    assert len(archetypes) >= 28
    ids = [a["archetype_id"] for a in archetypes]
    assert len(ids) == len(set(ids)), "archetype_id 必须唯一"


def test_archetype_required_fields_and_voice_format() -> None:
    voice_re = re.compile(r"^[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural$")
    rate_re = re.compile(r"^[+-]\d+%$")
    pitch_re = re.compile(r"^[+-]\d+Hz$")

    for archetype in load_archetypes():
        aid = archetype["archetype_id"]
        assert _REQUIRED_ARCHETYPE_FIELDS <= set(archetype), f"{aid} 缺字段"
        assert voice_re.match(archetype["voice_id"]), f"{aid} voice_id 非法: {archetype['voice_id']}"
        assert rate_re.match(archetype["prosody"]["rate"]), f"{aid} rate 非法"
        assert pitch_re.match(archetype["prosody"]["pitch"]), f"{aid} pitch 非法"

        locale = archetype["locale_info"]
        assert locale["country"], f"{aid} country 为空"
        assert locale["native_lang"], f"{aid} native_lang 为空"
        assert isinstance(locale["city_pool"], list) and len(locale["city_pool"]) >= 4, aid
        assert all(isinstance(c, str) and c.strip() for c in locale["city_pool"]), aid
        # 时区必须是合法 IANA 名称（ZoneInfo 可解析）
        ZoneInfo(locale["timezone"])

        vibe = archetype["vibe_constraints"]
        age_range = vibe["age_range"]
        assert isinstance(age_range, list) and len(age_range) == 2, f"{aid} age_range 非法"
        lo, hi = int(age_range[0]), int(age_range[1])
        assert 16 <= lo <= hi <= 80, f"{aid} age_range 非法: {age_range}"
        assert vibe["gender"] in ("male", "female"), f"{aid} gender 非法"
        assert isinstance(vibe["tone_tags"], list) and vibe["tone_tags"], f"{aid} tone_tags 为空"


def test_archetypes_cover_required_countries_and_have_fallback_pools() -> None:
    archetypes = load_archetypes()
    countries = {a["locale_info"]["country"] for a in archetypes}
    assert _REQUIRED_COUNTRIES <= countries, f"缺少国家: {_REQUIRED_COUNTRIES - countries}"

    gender_by_country: dict[str, set[str]] = {}
    for archetype in archetypes:
        aid = archetype["archetype_id"]
        # 每个原型都要有足够大的确定性兜底池
        name_pool = archetype.get("name_pool") or []
        assert len(name_pool) >= 8, f"{aid} name_pool 不足 8 个"
        assert all(isinstance(n, str) and n.strip() for n in name_pool), aid

        occupations = archetype.get("occupation_pool") or []
        assert len(occupations) >= 8, f"{aid} occupation_pool 不足 8 个"
        for occupation in occupations:
            assert isinstance(occupation, str) and len(occupation.strip()) >= 4, aid
            assert occupation.strip().lower() not in _GENERIC_OCCUPATIONS, (
                f"{aid} 职业太笼统: {occupation}"
            )

        assert archetype.get("speech_quirks"), f"{aid} 缺 speech_quirks"
        country = archetype["locale_info"]["country"]
        gender_by_country.setdefault(country, set()).add(archetype["vibe_constraints"]["gender"])

    # 每个国家都有男/女两种声线可选（否则 discover 过滤会过度降级）
    for country in sorted(_REQUIRED_COUNTRIES):
        assert gender_by_country[country] == {"male", "female"}, f"{country} 性别覆盖不足"


def test_multilingual_and_official_english_voices_are_preferred() -> None:
    archetypes = load_archetypes()
    voice_ids = [a["voice_id"] for a in archetypes]
    assert len(voice_ids) == len(set(voice_ids)), "声线不应重复"

    multilingual = [v for v in voice_ids if "MultilingualNeural" in v]
    assert len(multilingual) >= 6, f"MultilingualNeural 声线太少: {multilingual}"

    # 英语国家优先官方英语变体（en-XX-*），非英语国家才用外语声线读英文
    english_countries = {"United States", "United Kingdom", "Australia", "India", "Ireland"}
    for archetype in archetypes:
        country = archetype["locale_info"]["country"]
        voice = archetype["voice_id"]
        if country in english_countries:
            assert voice.startswith("en-"), f"{country} 应使用官方英语声线，实际 {voice}"


def test_multilingual_archetypes_ja_ko_pt_es_mx() -> None:
    """v2.1：新增的 8 个日本/韩国/巴西/墨西哥原型字段与真实声线。"""
    archetypes = {a["archetype_id"]: a for a in load_archetypes()}
    assert set(_MULTILINGUAL_ARCHETYPES) <= set(archetypes), (
        f"缺少原型: {set(_MULTILINGUAL_ARCHETYPES) - set(archetypes)}"
    )

    for aid, (voice_id, country, native_lang, timezone) in _MULTILINGUAL_ARCHETYPES.items():
        archetype = archetypes[aid]
        assert archetype["voice_id"] == voice_id
        locale = archetype["locale_info"]
        assert locale["country"] == country
        assert locale["native_lang"] == native_lang
        assert locale["timezone"] == timezone
        ZoneInfo(timezone)  # IANA 合法
        # 城市池非空且都在该时区语义下（时区本身已验证）
        assert len(locale["city_pool"]) >= 4
        # 确定性兜底池
        assert len(archetype["name_pool"]) >= 8
        assert len(archetype["occupation_pool"]) >= 8
        assert archetype["speech_quirks"]
        assert archetype["vibe_constraints"]["gender"] in ("male", "female")

    # 新声线全部唯一
    new_voices = [v[0] for v in _MULTILINGUAL_ARCHETYPES.values()]
    assert len(new_voices) == len(set(new_voices))
    all_voices = [a["voice_id"] for a in load_archetypes()]
    for voice in new_voices:
        assert all_voices.count(voice) == 1, f"声线重复: {voice}"

    # 日/韩原型口头禅应使用对应文字（便于无 LLM 破冰与 prompt 口吻）
    japanese_quirks = "".join(
        q for aid in ("jp_male_01", "jp_female_01") for q in archetypes[aid]["speech_quirks"]
    )
    assert re.search(r"[\u3040-\u30ff]", japanese_quirks)
    korean_quirks = "".join(
        q for aid in ("kr_male_01", "kr_female_01") for q in archetypes[aid]["speech_quirks"]
    )
    assert re.search(r"[\uac00-\ud7a3]", korean_quirks)


def test_archetype_by_id() -> None:
    first = load_archetypes()[0]
    found = archetype_by_id(first["archetype_id"])
    assert found is not None
    assert found["archetype_id"] == first["archetype_id"]
    assert found["voice_id"] == first["voice_id"]
    assert archetype_by_id("does_not_exist") is None
    assert archetype_by_id("") is None


def test_prosody_parsing_helpers() -> None:
    assert rate_to_speed("+10%") == pytest.approx(1.1)
    assert rate_to_speed("-20%") == pytest.approx(0.8)
    assert rate_to_speed("") == 1.0
    assert rate_to_speed("garbage") == 1.0
    assert rate_to_speed("+500%") == 2.0  # 夹到安全范围

    assert pitch_to_hz("+4Hz") == pytest.approx(4.0)
    assert pitch_to_hz("-3Hz") == pytest.approx(-3.0)
    assert pitch_to_hz("") == 0.0
    assert pitch_to_hz("nope") == 0.0


# --------------------------------------------------------------------------- #
# FriendVoiceSynth
# --------------------------------------------------------------------------- #
def _fake_wave(duration_s: float = 0.25, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(duration_s * SR), dtype=np.float32) / SR
    return (0.2 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


@pytest.fixture
def edge_calls(monkeypatch):
    """monkeypatch EdgeTTSEngine.synthesize_voice（离线），返回调用记录列表。"""
    calls: list[dict[str, object]] = []

    async def fake_synthesize_voice(self, text, voice, *, rate="+0%", pitch="+0Hz"):
        calls.append({"text": text, "voice": voice, "rate": rate, "pitch": pitch})
        return _fake_wave()

    monkeypatch.setattr(EdgeTTSEngine, "synthesize_voice", fake_synthesize_voice)
    return calls


def _assert_valid_wav(path: str) -> None:
    assert os.path.isabs(path)
    assert os.path.isfile(path)
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == SR
        assert wf.getnframes() > 0


async def test_synth_to_wav_writes_24k_wav(tmp_path, edge_calls) -> None:
    synth = FriendVoiceSynth()
    out_dir = str(tmp_path / "audio")
    path, duration_ms = await synth.synth_to_wav(
        "Hey, how's it going?", "en-GB-RyanNeural", out_dir=out_dir
    )

    _assert_valid_wav(path)
    assert os.path.dirname(path) == os.path.abspath(out_dir)
    assert 200 <= duration_ms <= 300, duration_ms
    assert len(edge_calls) == 1
    assert edge_calls[0] == {
        "text": "Hey, how's it going?",
        "voice": "en-GB-RyanNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
    }


async def test_synth_forwards_rate_and_pitch(tmp_path, edge_calls) -> None:
    synth = FriendVoiceSynth()
    await synth.synth_to_wav(
        "Allora, let's go.", "it-IT-GiuseppeMultilingualNeural",
        rate="+8%", pitch="-2Hz", out_dir=str(tmp_path),
    )
    assert edge_calls[0]["rate"] == "+8%"
    assert edge_calls[0]["pitch"] == "-2Hz"


async def test_synth_cache_hits_skip_edge(tmp_path, edge_calls) -> None:
    synth = FriendVoiceSynth()
    path1, dur1 = await synth.synth_to_wav("Cheers mate.", "en-AU-NatashaNeural", out_dir=str(tmp_path))
    path2, dur2 = await synth.synth_to_wav("Cheers mate.", "en-AU-NatashaNeural", out_dir=str(tmp_path))

    assert path1 == path2
    assert dur1 == dur2
    assert len(edge_calls) == 1, "缓存命中时不应再合成"
    assert synth.cache_stats()["entries"] == 1

    # 不同文本/不同韵律参数 -> 缓存未命中
    await synth.synth_to_wav("Cheers mate.", "en-AU-NatashaNeural", rate="+5%", out_dir=str(tmp_path))
    await synth.synth_to_wav("Something else.", "en-AU-NatashaNeural", out_dir=str(tmp_path))
    assert len(edge_calls) == 3


async def test_synth_falls_back_to_tone_when_edge_fails(tmp_path, monkeypatch) -> None:
    async def boom(self, text, voice, *, rate="+0%", pitch="+0Hz"):
        raise RuntimeError("network down")

    monkeypatch.setattr(EdgeTTSEngine, "synthesize_voice", boom)
    synth = FriendVoiceSynth()
    path, duration_ms = await synth.synth_to_wav(
        "No worries, we can still talk.", "en-IE-ConnorNeural", out_dir=str(tmp_path)
    )

    _assert_valid_wav(path)
    assert duration_ms > 0
    assert os.path.getsize(path) > 44  # 大于 wav 头


async def test_synth_treats_empty_edge_audio_as_failure(tmp_path, monkeypatch) -> None:
    async def empty(self, text, voice, *, rate="+0%", pitch="+0Hz"):
        return np.zeros(0, dtype=np.float32)

    monkeypatch.setattr(EdgeTTSEngine, "synthesize_voice", empty)
    synth = FriendVoiceSynth()
    path, duration_ms = await synth.synth_to_wav(
        "Hi there!", "en-US-AvaMultilingualNeural", out_dir=str(tmp_path)
    )
    _assert_valid_wav(path)
    assert duration_ms > 0


async def test_synth_rejects_empty_text_and_voice(tmp_path) -> None:
    synth = FriendVoiceSynth()
    with pytest.raises(ValueError):
        await synth.synth_to_wav("   ", "en-GB-RyanNeural", out_dir=str(tmp_path))
    with pytest.raises(ValueError):
        await synth.synth_to_wav("hello", "", out_dir=str(tmp_path))
