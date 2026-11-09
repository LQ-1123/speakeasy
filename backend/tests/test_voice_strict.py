from types import SimpleNamespace

import pytest

from app.voice.voice_registry import binding_for_persona


def test_strict_binding_rejects_unready_persona_without_default_voice():
    with pytest.raises(ValueError, match="voice_not_ready"):
        binding_for_persona({"id": "p_wait", "status": "waiting_voice", "language": "ja"}, strict=True)


def test_strict_binding_never_switches_fish_identity_to_edge():
    cfg = SimpleNamespace(provider="edge", fish_api_key="")
    binding = binding_for_persona(
        {"id": "p_ready", "voice_id": "en-GB-RyanNeural", "fish_voice_id": "fish-owned"},
        cfg, strict=True,
    )
    assert binding.provider == "fish"
