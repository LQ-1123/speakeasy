"""Fish 合成结果磁盘缓存：同一角色 + 同一段文字只调用一次。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.voice import fish_tts, voice_registry


def _binding() -> voice_registry.VoiceBinding:
    return voice_registry.VoiceBinding(
        persona_id="p-test",
        provider="fish",
        ref="ref-test",
        edge_voice="en-GB-RyanNeural",
        token="fish-cache-test",
    )


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        provider="fish",
        fish_api_key="test-key",
        fish_base_url="https://fish.example",
        fish_model="s2.1-pro",
        fish_format="mp3",
    )


@pytest.mark.asyncio
async def test_synthesize_reuses_disk_cache(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_synthesize(*args, **kwargs) -> bytes:
        calls["n"] += 1
        return b"fish-audio" * 300

    monkeypatch.setattr(fish_tts, "synthesize", fake_synthesize)
    monkeypatch.setenv("SE_TTS_FISH", "1")
    voice_registry.reset_state()

    first = await voice_registry.synthesize(
        {"id": "p-test"},
        "今天过得怎么样？",
        binding=_binding(),
        cfg=_cfg(),
        audio_format="mp3",
        out_dir=str(tmp_path),
    )
    second = await voice_registry.synthesize(
        {"id": "p-test"},
        "今天过得怎么样？",
        binding=_binding(),
        cfg=_cfg(),
        audio_format="mp3",
        out_dir=str(tmp_path),
    )

    assert first is not None and first.data
    assert second is not None and second.data == first.data
    assert calls["n"] == 1
    assert (tmp_path / voice_registry._TTS_CACHE_DIR).is_dir()
