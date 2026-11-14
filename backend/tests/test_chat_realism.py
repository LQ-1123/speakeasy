"""Regressions for contradictory persona instructions and offline context handling."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.v2.chat_service import ChatService, _clean_reply
from app.v3 import behavior, first_message, prompt_composer


@pytest.mark.parametrize(
    "message, expected",
    [
        ("My dog died yesterday", "Oh no. I'm sorry."),
        ("Good night, talk tomorrow", "Talk later."),
        ("Are you an AI?", "I'm an AI character here in Sonder."),
        ("你是真人吗？", "I'm an AI character here in Sonder."),
    ],
)
def test_offline_response_respects_message_before_random_behavior(message, expected):
    chat = object.__new__(ChatService)
    plan = SimpleNamespace(behavior="tell_story", dna_traits={"verbosity": 1.0})
    assert chat._mock_reply({"reply_language": "en", "speech_quirks": ["mate"]}, message, plan=plan) == expected


@pytest.mark.parametrize("language", first_message.SUPPORTED_LANGUAGES)
def test_opening_never_falls_back_to_english_for_supported_language(language):
    variants = first_message.first_message_variants({"id": "new-person", "personality": "reserved"}, language=language)
    assert variants
    if language not in ("en", "zh"):
        assert all(line in first_message._LOCALIZED_OPENERS[language] for line in variants)


def test_emergency_prompt_keeps_truthful_identity_and_does_not_invent_anecdotes():
    prompt = "\n".join(m.text for m in prompt_composer._emergency_modules("call", "en", {"name": "Sam"}, None))
    assert "answer honestly" in prompt
    assert "never an AI" not in prompt
    assert "don't invent an anecdote" in prompt


def test_social_behavior_does_not_require_missing_personal_history():
    for action in ("share", "tell_story", "complain"):
        text = behavior.behavior_directive(
            behavior.BehaviorChoice(action, "test", 1.0, {}), dna_traits={}
        )
        assert "actual message takes priority" in text
        assert "established" in text or "recorded" in text


def test_call_prompt_fallback_preserves_identity_honesty(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("composer unavailable")
    monkeypatch.setattr(prompt_composer, "compose_call_prompt", fail)
    prompt = object.__new__(ChatService)._call_system_prompt({"name": "Sam", "reply_language": "ja"}, None)
    assert "answer honestly" in prompt
    assert "Japanese" in prompt
    assert "Never say you are an AI" not in prompt


def test_reply_cleanup_preserves_answer_after_old_character_limit():
    reply = "Here is the background. " * 30 + "But I haven't booked the trip."
    assert _clean_reply(reply).endswith("But I haven't booked the trip.")


@pytest.mark.parametrize("mode", ["text", "call"])
def test_composer_uses_turn_time_and_does_not_reanimate_finished_activity(mode):
    plan = SimpleNamespace(
        moment=datetime(2026, 11, 11, 12, tzinfo=timezone.utc),
        persona={"nickname": "Sam", "timezone": "UTC", "personality": "Quiet and concise."},
        activity={"label": "at the dentist", "event_status": "ongoing",
                  "start_time": "2026-11-11T08:00:00Z", "end_time": "2026-11-11T09:00:00Z"},
    )
    result = prompt_composer.compose_prompt(None, {"name": "Sam"}, mode=mode, plan=plan)
    scene = result.module("scenario").text
    assert "at the dentist" not in scene
    assert "12:00" in scene


def test_life_facts_keep_complete_fact_and_status():
    block = "[Your plans: not happened yet]\n- PLAN: " + "a" * 100
    block += "\n[Your life: things that already happened]\n- DONE: Sent the draft."
    context = SimpleNamespace(plan=SimpleNamespace(life_block=block))
    kept = prompt_composer._life_facts_block(context, 90)
    assert kept == "[Your life: things that already happened]\n- DONE: Sent the draft."


def test_canonical_personality_replaces_stale_friend_prompt():
    plan = SimpleNamespace(persona={"nickname": "Sam", "personality": "Quiet and concise."})
    result = prompt_composer.compose_prompt(
        None, {"name": "Sam", "personality_prompt": "Always ask three questions and use lots of emoji."}, plan=plan
    )
    card = result.module("character_card").text
    assert "Quiet and concise" in card
    assert "Always ask three questions" not in card


def test_long_profile_does_not_evict_recent_episode_and_pending_thread():
    memory = "[What you remember about them — UserFactSheet]\n- Profile: " + "x" * 500
    memory += "\n[SharedEpisodes]\n- Old chat.\n- Yesterday: decided to keep the bookmark."
    memory += "\n[OpenLoops — pending]\n- Ask about the interview when relevant.\n[Rules]\nRules."
    result = prompt_composer._trim_memory_context(memory, 180)
    assert "decided to keep the bookmark" in result
    assert "interview" in result
    assert "x" * 20 not in result
    assert len(result) <= 180
