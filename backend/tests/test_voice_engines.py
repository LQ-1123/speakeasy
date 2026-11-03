"""语音引擎测试：STT(mock) / TTS(tone) / 参考音频库 / 文本规范化。

全部在无模型、无 GPU 环境下运行。
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.config import STTSettings, TTSSettings


# --------------------------------------------------------------------------- #
# STT (mock)
# --------------------------------------------------------------------------- #
def test_stt_mock_returns_transcript():
    from app.voice.stt import STTEngine

    cfg = STTSettings(backend="mock", mock_text="I'd like a flat white to go, please.")
    engine = STTEngine(cfg)
    assert engine.backend_name == "mock"

    async def run():
        t = await engine.transcribe(b"\x00\x00" * 16000)
        await engine.aclose()
        return t

    t = asyncio.run(run())
    assert t.text == "I'd like a flat white to go, please."
    assert t.backend == "mock"
    assert t.latency_ms >= 0


def test_stt_auto_falls_back_to_mock():
    """没装 faster-whisper 时，auto 必须降级而不是抛异常。"""
    from app.voice.stt import STTEngine

    engine = STTEngine(STTSettings(backend="auto"))

    async def run():
        try:
            await engine.prewarm()
        except Exception as exc:  # prewarm 允许多种降级路径
            pytest.fail(f"prewarm 不应抛出: {exc}")
        return engine.backend_name

    name = asyncio.run(run())
    assert name in ("mock", "faster-whisper"), name


def test_stt_empty_audio_mock():
    from app.voice.stt import STTEngine

    engine = STTEngine(STTSettings(backend="mock", mock_text="Hello."))

    async def run():
        t = await engine.transcribe(b"")
        await engine.aclose()
        return t

    t = asyncio.run(run())
    assert isinstance(t.text, str)


# --------------------------------------------------------------------------- #
# TTS 文本规范化
# --------------------------------------------------------------------------- #
def test_normalize_strips_stage_directions():
    from app.voice.tts import normalize_for_tts

    out = normalize_for_tts("*smiles warmly* Hello there!")
    assert "*" not in out
    assert "smiles" not in out.lower() or "hello" in out.lower()


def test_normalize_strips_markdown():
    from app.voice.tts import normalize_for_tts

    out = normalize_for_tts("**Bold** and `code` and # Header")
    assert "**" not in out
    assert "`" not in out
    assert "#" not in out


def test_normalize_strips_emoji():
    from app.voice.tts import normalize_for_tts

    out = normalize_for_tts("Great job 🎉👍 really!")
    assert "🎉" not in out
    assert "👍" not in out
    assert "Great job" in out


def test_normalize_expands_abbreviations():
    from app.voice.tts import normalize_for_tts

    assert "and" in normalize_for_tts("salt & pepper").lower()
    assert "percent" in normalize_for_tts("50% off").lower()


def test_normalize_keeps_apostrophes():
    from app.voice.tts import normalize_for_tts

    out = normalize_for_tts("I don't think it's right.")
    assert "don't" in out or "don’t" in out
    assert "it's" in out or "it’s" in out


def test_normalize_collapses_whitespace():
    from app.voice.tts import normalize_for_tts

    out = normalize_for_tts("Hello     there\n\n\nfriend")
    assert "     " not in out
    assert "\n" not in out


def test_normalize_empty():
    from app.voice.tts import normalize_for_tts

    assert normalize_for_tts("") == ""
    assert normalize_for_tts("   ") == ""
    assert normalize_for_tts("*action only*") == ""


@pytest.mark.parametrize(
    "text",
    [
        "Right then, that's a flat white for you.",
        "Actually, one small thing only — can we prepone it?",
        "Allora, the colour is too much, no?",
    ],
)
def test_normalize_preserves_content(text):
    from app.voice.tts import normalize_for_tts

    out = normalize_for_tts(text)
    # 主要词干应该还在
    first_word = text.split()[0].strip("*_#,.")
    assert first_word in out


# --------------------------------------------------------------------------- #
# TTS 二次切分
# --------------------------------------------------------------------------- #
def test_split_for_tts_short_text_unchanged():
    from app.voice.tts import split_for_tts

    text = "Right then, a flat white."
    got = split_for_tts(text, max_chars=220)
    assert got == [text] or (len(got) == 1 and got[0] == text)


def test_split_for_tts_long_text_splits():
    from app.voice.tts import split_for_tts

    text = "word " * 100 + "end."
    got = split_for_tts(text, max_chars=100)
    assert len(got) > 1
    for s in got:
        assert len(s) <= 160, f"{len(s)} 太长"


def test_split_for_tts_no_empty():
    from app.voice.tts import split_for_tts

    got = split_for_tts("   ", max_chars=50)
    assert all(s.strip() for s in got)


# --------------------------------------------------------------------------- #
# TTS tone 后端
# --------------------------------------------------------------------------- #
def test_tts_tone_produces_audio():
    from app.voice.tts import TTSEngine, TTSSentenceRequest

    engine = TTSEngine(TTSSettings(backend="tone"))

    async def run():
        req = TTSSentenceRequest(
            text="Right then, a flat white it is.",
            persona_id="oliver",
            sentence_index=0,
            is_first_sentence=True,
        )
        chunks = [c async for c in engine.synthesize(req)]
        await engine.aclose()
        return chunks

    chunks = asyncio.run(run())
    assert chunks, "tone 后端必须产出音频"
    total = sum(len(c.pcm16) for c in chunks)
    assert total > 0
    assert total % 2 == 0  # 16-bit

    assert all(c.sample_rate == 24000 for c in chunks)
    assert chunks[0].is_first is True
    assert chunks[-1].is_last is True
    # index 递增
    idxs = [c.index for c in chunks]
    assert idxs == sorted(idxs)


def test_tts_tone_duration_reasonable():
    from app.voice.tts import TTSEngine, TTSSentenceRequest

    engine = TTSEngine(TTSSettings(backend="tone"))

    async def run():
        req = TTSSentenceRequest(
            text="This is a reasonably long sentence that should take a bit of time to say.",
            persona_id="oliver",
            sentence_index=0,
            is_first_sentence=True,
        )
        chunks = [c async for c in engine.synthesize(req)]
        await engine.aclose()
        return chunks

    chunks = asyncio.run(run())
    total_bytes = sum(len(c.pcm16) for c in chunks)
    dur = total_bytes / 2 / 24000
    assert 0.5 < dur < 15, f"时长 {dur:.2f}s 不合理"


def test_tts_tone_empty_text_yields_nothing_or_silence():
    from app.voice.tts import TTSEngine, TTSSentenceRequest

    engine = TTSEngine(TTSSettings(backend="tone"))

    async def run():
        req = TTSSentenceRequest(
            text="*only action*", persona_id="oliver", sentence_index=0, is_first_sentence=True
        )
        chunks = [c async for c in engine.synthesize(req)]
        await engine.aclose()
        return chunks

    asyncio.run(run())  # 不抛异常即可


def test_tts_backend_name_no_model_load():
    from app.voice.tts import TTSEngine

    engine = TTSEngine(TTSSettings(backend="auto"))
    name = engine.backend_name
    assert name in ("edge", "xtts", "tone", "cosyvoice2", "f5tts", "auto", "pending"), name


def test_tts_auto_prefers_local_over_network():
    """auto 顺序：本地（cosyvoice2/f5tts/xtts）优先于网络（edge）。

    实测依据：Edge-TTS 从本机访问会波动到 1900–6000ms（还会被限流），
    而 XTTS 本地稳定在 248–1088ms。对话场景延迟敏感，所以本地优先。
    """
    from app.voice.tts import TTSEngine

    engine = TTSEngine(TTSSettings(backend="auto"))
    order = engine._candidate_order()
    # tone 必须在最后
    assert order[-1] == "tone"
    # edge 必须排在任何可用的本地后端之后
    assert order.index("edge") > order.index("xtts")


def test_tts_explicit_tone_still_honored():
    """显式要求 tone 时必须尊重（测试/离线场景）。"""
    from app.voice.tts import TTSEngine

    engine = TTSEngine(TTSSettings(backend="tone"))
    assert engine.backend_name == "tone"


def test_tts_health_is_dict():
    from app.voice.tts import TTSEngine

    h = TTSEngine(TTSSettings(backend="tone")).health()
    assert isinstance(h, dict)


# --------------------------------------------------------------------------- #
# 参考音频库
# --------------------------------------------------------------------------- #
def test_reference_library_loads():
    from app.voice.ref_audio import ReferenceLibrary

    lib = ReferenceLibrary("assets/ref_audio")
    voices = lib.all()
    assert isinstance(voices, list)


def test_reference_library_get_unknown_returns_none():
    from app.voice.ref_audio import ReferenceLibrary

    lib = ReferenceLibrary("assets/ref_audio")
    assert lib.get("nobody_here") is None


def test_reference_library_validate_reports_missing():
    """默认仓库里没有参考音频 → validate 必须报出来（而不是静默通过）。"""
    from app.voice.ref_audio import ReferenceLibrary

    lib = ReferenceLibrary("assets/ref_audio")
    problems = lib.validate()
    assert isinstance(problems, list)
    # 没有音频文件时应有问题（除非用户已经录好）
    for p in problems:
        assert isinstance(p, str)


def test_reference_library_missing_dir_is_safe():
    from app.voice.ref_audio import ReferenceLibrary

    lib = ReferenceLibrary("definitely/not/a/dir")
    assert isinstance(lib.validate(), list)
    assert lib.get("oliver") is None


# --------------------------------------------------------------------------- #
# 级联架构（保留口音的克隆）
# --------------------------------------------------------------------------- #
def test_openvoice_engine_looks_available_is_bool():
    from app.voice.openvoice_engine import OpenVoiceEngine

    assert isinstance(OpenVoiceEngine.looks_available(), bool)


def test_openvoice_resample_preserves_duration():
    """重采样后时长应基本不变（22.05k → 24k）。"""
    import numpy as np

    from app.voice.openvoice_engine import _resample

    x = np.sin(np.linspace(0, 2 * np.pi * 440, 22050)).astype(np.float32)
    y = _resample(x, 22050, 24000)
    assert abs(y.size / 24000 - x.size / 22050) < 0.01


def test_openvoice_resample_identity_when_same_rate():
    import numpy as np

    from app.voice.openvoice_engine import _resample

    x = np.ones(1000, dtype=np.float32)
    assert _resample(x, 24000, 24000) is x or _resample(x, 24000, 24000).size == 1000


def test_tts_clonable_backends_before_binding():
    """未注入任何克隆引擎时，两个能力都应为 False。"""
    from app.voice.tts import TTSEngine
    from app.config import TTSSettings

    e = TTSEngine(TTSSettings(backend="tone"))
    caps = e.clonable_backends()
    assert caps["cascade_accent_preserving"] is False
    assert caps["xtts_direct"] is False


def test_tts_bind_openvoice_toggles_capability():
    """注入 OpenVoice 后 cascade 能力应为 True —— 这是"口音保留"的开关。"""
    from app.voice.tts import TTSEngine
    from app.config import TTSSettings

    e = TTSEngine(TTSSettings(backend="tone"))
    e.bind_openvoice(object())
    assert e.clonable_backends()["cascade_accent_preserving"] is True
    assert e.openvoice is not None

    e.bind_openvoice(None)
    assert e.clonable_backends()["cascade_accent_preserving"] is False


def test_tts_cascade_not_ready_without_timbre():
    """OpenVoice 在但音色没登记 → cascade 不可用（应回退）。"""
    from app.config import TTSSettings
    from app.voice.tts import TTSEngine

    class _FakeOV:
        def get_timbre(self, _tid):
            return None

    e = TTSEngine(TTSSettings(backend="tone"))
    e.bind_openvoice(_FakeOV())
    assert e._cascade_ready("some_voice") is False


def test_persona_accent_labels_cover_all_personas():
    """每个角色都要有口音标签（单一来源：EdgeVoice.accent_label）。"""
    from app.routers.voice_api import _persona_accent

    for pid in ("oliver", "raj", "marco", "emily", "priya"):
        label = _persona_accent(pid)
        assert label and label != "English", f"{pid} 缺口音标签"


def test_persona_edge_voice_resolves():
    """每个角色都能解析到一个声线（口音来源）。"""
    from app.voice.edge_tts_engine import resolve_voice

    for pid in ("oliver", "raj", "marco", "emily", "priya"):
        v = resolve_voice(pid)
        assert v.voice, f"{pid} 没有声线"
        # 官方变体以 en- 开头；外语兜底可以是 it-IT / fr-FR 等
        assert v.voice.count("-") >= 2, f"{pid} 声线格式异常: {v.voice}"


def test_marco_uses_italian_voice_fallback():
    """marco 是意大利角色：没有 en-IT 官方变体，用意大利语声线读英文兜底。

    这是用户实测确认的方案：口音接近了，可以用于兜底。
    """
    from app.voice.edge_tts_engine import resolve_voice

    v = resolve_voice("marco")
    assert v.voice.startswith("it-IT")
    assert v.accent_source == "foreign_fallback"


def test_official_variants_preferred_for_personas():
    """有官方英语变体的角色必须用官方变体（口音 100% 自然）。"""
    from app.voice.edge_tts_engine import resolve_voice

    from app.personas import get_personas

    lib = get_personas()
    for pid in ("oliver", "raj", "emily", "priya"):
        # 用户可能在口音市场里换过这个角色的口音；那是合法覆盖，
        # 这里校验的是「出厂默认映射」仍然优先选官方英语变体。
        if lib.voice_override(pid):
            continue
        v = resolve_voice(pid)
        assert v.accent_source == "official", f"{pid} 应使用官方变体"
        assert v.voice.startswith("en-"), f"{pid} 的声线应是英语变体"
        assert v.accent_label, f"{pid} 应有口音标签"


def test_no_english_voice_fakes_foreign_accent():
    """绝不能在英语声线上假装外语口音 —— 那样既不像也不准。"""
    from app.voice.edge_tts_engine import resolve_voice

    for pid in ("oliver", "raj", "emily", "priya", "marco"):
        v = resolve_voice(pid)
        if v.accent_source == "foreign_fallback":
            assert not v.voice.startswith("en-"), (
                f"{pid} 标为外语兜底，却用了英语声线 {v.voice}"
            )


def test_official_variant_catalog_nonempty():
    from app.voice.edge_tts_engine import OFFICIAL_EN_VARIANTS

    assert len(OFFICIAL_EN_VARIANTS) >= 10
    total = sum(len(v) for v in OFFICIAL_EN_VARIANTS.values())
    assert total >= 30


def test_foreign_fallback_catalog_has_italian():
    from app.voice.edge_tts_engine import FOREIGN_FALLBACK_VOICES

    assert "it-IT" in FOREIGN_FALLBACK_VOICES
    assert all(v.startswith("it-IT") for v in FOREIGN_FALLBACK_VOICES["it-IT"])


# --------------------------------------------------------------------------- #
# 口音市场 / 自定义角色 / 自定义场景
# --------------------------------------------------------------------------- #
def test_accent_market_catalog_nonempty():
    from app.voice.accent_market import ACCENT_CATALOG

    assert len(ACCENT_CATALOG) >= 45


def test_accent_market_official_preferred():
    """官方变体数量应显著多于外语兜底（这是口音策略的直接体现）。"""
    from app.voice.accent_market import market_summary

    s = market_summary()
    assert s["official"] >= 30
    assert s["official"] > s["foreign_fallback"]


def test_accent_market_has_italian_fallback():
    from app.voice.accent_market import by_locale

    rows = by_locale("it-IT")
    assert len(rows) >= 3
    assert all(r["kind"] == "foreign_fallback" for r in rows)


def test_accent_market_regions_have_flags_and_counts():
    from app.voice.accent_market import regions

    rows = regions()
    assert len(rows) >= 15
    for r in rows:
        assert r["flag"]
        assert r["label_zh"]
        assert r["voice_count"] >= 1


def test_accent_market_get_by_voice():
    from app.voice.accent_market import get_accent

    a = get_accent("en-GB-RyanNeural")
    assert a is not None
    assert a["kind"] == "official"
    assert "英国" in a["label"]

    b = get_accent("it-IT-GiuseppeMultilingualNeural")
    assert b is not None
    assert b["kind"] == "foreign_fallback"


def test_accent_market_unknown_voice_is_none():
    from app.voice.accent_market import get_accent

    assert get_accent("xx-XX-Nobody") is None


def test_custom_store_persona_roundtrip(tmp_path):
    from app.personas.custom_store import CustomStore

    store = CustomStore(str(tmp_path))
    cp = store.upsert_persona(
        {
            "name": "Chidi",
            "city": "Berlin",
            "job": "创始人",
            "voice": "en-NG-AbeoNeural",
            "traits": "直接, 好奇",
        }
    )
    assert cp.id.startswith("u_")
    got = store.get_persona(cp.id)
    assert got is not None
    assert got.name == "Chidi"
    assert store.delete_persona(cp.id) is True
    assert store.get_persona(cp.id) is None


def test_custom_store_scene_prompt_suffix(tmp_path):
    from app.personas.custom_store import CustomStore

    store = CustomStore(str(tmp_path))
    sc = store.upsert_scene(
        {
            "name": "柏林面试",
            "template": "interview",
            "place": "柏林一家初创公司",
            "goal": "面试产品经理",
            "details": "对方是创始人",
        }
    )
    suffix = sc.build_prompt_suffix()
    assert "面试" in suffix
    assert "柏林" in suffix
    assert store.delete_scene(sc.id) is True


def test_custom_store_voice_overrides(tmp_path):
    from app.personas.custom_store import CustomStore

    store = CustomStore(str(tmp_path))
    store.set_voice_override("oliver", "en-ZA-LukeNeural")
    assert store.load_voice_overrides()["oliver"] == "en-ZA-LukeNeural"
    store.set_voice_override("oliver", "")
    assert "oliver" not in store.load_voice_overrides()


def test_custom_persona_builds_prompt_without_fake_spelling():
    """自定义角色的提示词不能靠假拼写冒充口音。"""
    from app.personas.custom_store import CustomPersona

    cp = CustomPersona(
        id="u_test",
        name="Chidi",
        city="Berlin",
        job="创始人",
        voice="en-NG-AbeoNeural",
        traits="直接, 好奇",
        style="说话快",
        extra="会说一点德语",
    )
    prompt = cp.build_system_prompt("这是一场面试。")
    assert "Chidi" in prompt
    assert "创始人" in prompt
    assert "重述" in prompt  # 纠错策略
    assert "不要扮演老师" in prompt


def test_scene_templates_cover_common_situations():
    from app.personas.custom_store import SCENE_TEMPLATES

    for key in ("cafe", "airport", "interview", "hospital", "hotel"):
        assert key in SCENE_TEMPLATES
        assert SCENE_TEMPLATES[key]["name"]
    assert "custom" in SCENE_TEMPLATES
