import asyncio
from types import SimpleNamespace

import pytest

from app.v3.call_runtime import CallRuntime, reply_audio


class Chat:
    def __init__(self):
        self.engines = SimpleNamespace(llm=SimpleNamespace(uses_mock=False, stream_chat=self.stream))
        self.prompts = []

    def _call_turn_plan(self, friend, text):
        return None

    def _call_system_prompt(self, friend, plan):
        return "Be a friend."

    async def stream(self, messages, **kw):
        self.prompts.append(messages)
        yield "Hello there. "
        yield "How are you?"

    async def _synth_sentence(self, text, friend):
        return {"url": "/audio/test.wav", "duration_ms": 100, "path": "test.wav"}


@pytest.mark.asyncio
async def test_generation_rejects_mock_models_in_real_calls():
    chat = Chat()
    chat.engines.llm.uses_mock = True
    with pytest.raises(RuntimeError, match="model-unavailable"):
        _ = [item async for item in reply_audio(chat, {}, "hi", [])]


@pytest.mark.asyncio
async def test_failed_synthesis_never_becomes_successful_empty_audio():
    chat = Chat()
    async def fail(*args):
        return None
    chat._synth_sentence = fail
    with pytest.raises(RuntimeError, match="speech-unavailable"):
        _ = [item async for item in reply_audio(chat, {}, "hi", [])]


@pytest.mark.asyncio
async def test_synthesis_does_not_block_reading_remaining_model_tokens():
    chat = Chat()
    consumed = asyncio.Event()
    async def stream(*args, **kwargs):
        yield "First sentence. "
        yield "Second sentence."
        consumed.set()
    async def synth(text, friend):
        await asyncio.wait_for(consumed.wait(), .5)
        return {"url": "/audio/test.wav", "duration_ms": 100}
    chat.engines.llm.stream_chat = stream
    chat._synth_sentence = synth
    items = [item async for item in reply_audio(chat, {}, "hi", [])]
    assert [item["text"] for item in items] == ["First sentence.", "Second sentence."]


def test_only_played_current_turn_audio_is_added_to_context():
    runtime = CallRuntime()
    assert runtime.begin_turn("one", "Hello")
    runtime.offer_audio("a", "one", "Hi", 0)
    runtime.offer_audio("b", "one", "How are you?", 1)
    assert runtime.acknowledge("a", "one")
    assert not runtime.acknowledge("a", "one")
    runtime.interrupt()
    assert not runtime.acknowledge("b", "one")
    assert runtime.history == [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]
    assert not runtime.begin_turn("one", "duplicate request")


def test_out_of_order_playback_ack_cannot_create_false_context():
    runtime = CallRuntime()
    runtime.begin_turn("one", "Hello")
    runtime.offer_audio("a", "one", "Hi", 0)
    runtime.offer_audio("b", "one", "How are you?", 1)
    assert not runtime.acknowledge("b", "one")
    assert runtime.acknowledge("a", "one")
    assert runtime.acknowledge("b", "one")
