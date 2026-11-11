"""Fish 情绪标记：对用户隐藏、不读出来，但 Fish 合成时保留。

规则：
- 白名单 `[excited]` 等给 Fish 表演；
- 显示文本 / Edge 文本必须去掉（Edge 会照字面念 "excited"）；
- 用户自己写的 `[哈哈]` 不在白名单，不能被误吞。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.v2.chat_service import ChatService, split_emotion, strip_emotion_tags


def test_inline_emotion_tag_hidden_but_kept_for_fish():
    display, tts = split_emotion("hi [excited]！")
    assert display == "hi！"
    assert "[excited]" not in display
    assert tts.startswith("[excited] ")


def test_emotion_tag_at_start_or_middle():
    for raw in ("[excited] hello there!", "I got the job [excited]!"):
        display, tts = split_emotion(raw)
        assert "[excited]" not in display
        assert "[excited]" in tts
        assert display.strip()


def test_unknown_bracket_content_is_not_swallowed():
    raw = "hi [哈哈]！"
    display, tts = split_emotion(raw)
    assert display == raw
    assert tts == raw


def test_multiple_tags_are_capped_and_prefixed():
    display, tts = split_emotion("[excited][laughing][soft][sad] hello")
    assert display == "hello"
    assert tts.startswith("[excited] [laughing] [soft] ")
    assert "[sad]" not in tts  # 最多 3 个，避免 prompt 里堆标记


def test_strip_emotion_tags_cleans_edge_text():
    assert strip_emotion_tags("[excited] hi!") == "hi!"
    assert strip_emotion_tags("hi [long-break] there") == "hi there"
    assert strip_emotion_tags("hi [哈哈]") == "hi [哈哈]"


def test_edge_fallback_never_reads_tags(tmp_path):
    """Fish 不可用时回落 Edge，Edge 收到的文本里不能有 [excited]。"""
    captured: list[str] = []

    class _Voice:
        async def synth_to_wav(self, text: str, voice: str, **_kwargs: Any):
            captured.append(text)
            return str(tmp_path / "x.wav"), 1200

    class _Audio:
        audio_dir = str(tmp_path)

    service = object.__new__(ChatService)
    service.audio = _Audio()
    service.voice = _Voice()
    service.tts_config = None
    service._voice_binding = lambda friend: None  # type: ignore[method-assign]

    async def _no_fish(text: str, emotion: str, friend: dict | None = None):
        return None

    service._fish_voice = _no_fish  # type: ignore[method-assign]

    result = asyncio.run(
        service._synth_sentence("[excited] hi there!", {"voice_id": "en-US-TestNeural"})
    )
    assert result is not None
    assert captured == ["hi there!"]
