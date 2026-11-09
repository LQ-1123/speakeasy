from types import SimpleNamespace
import pytest

from app.v2.voices import FriendVoiceSynth
from app.v2.chat_service import ChatService


class FailedEngine:
    async def synthesize_voice(self, *a, **kw):
        raise RuntimeError('provider-offline')


@pytest.mark.asyncio
async def test_strict_edge_failure_never_produces_tone(tmp_path, monkeypatch):
    monkeypatch.setenv('SE_VOICE_STRICT','1')
    synth=FriendVoiceSynth(engine=FailedEngine())
    with pytest.raises(RuntimeError,match='provider-offline'):
        await synth.synth_to_wav('hello','en-GB-RyanNeural',out_dir=str(tmp_path))
    assert list(tmp_path.iterdir())==[]


@pytest.mark.asyncio
async def test_strict_fish_failure_never_calls_edge(tmp_path, monkeypatch):
    monkeypatch.setenv('SE_VOICE_STRICT','1')
    class NoEdge:
        async def synth_to_wav(self,*args,**kwargs):
            pytest.fail('A Fish persona must never change voice to Edge')
    chat=ChatService(engines=SimpleNamespace(),db=None,memory=None,voice=NoEdge(),spawner=None,
                     audio_store=SimpleNamespace(audio_dir=str(tmp_path)))
    async def no_fish(*args,**kwargs): return None
    chat._fish_voice=no_fish
    chat._voice_binding=lambda friend:SimpleNamespace(strict=True,fish_enabled=True,edge_voice='en-GB-RyanNeural')
    assert await chat._synth_sentence('hello',{'id':'f'}) is None


@pytest.mark.asyncio
async def test_private_call_synthesis_cannot_reuse_public_chat_audio(tmp_path):
    import numpy as np
    from pathlib import Path
    class Engine:
        async def synthesize_voice(self,*args,**kwargs): return np.ones(240,dtype=np.float32)*.1
    synth=FriendVoiceSynth(engine=Engine())
    public,_=await synth.synth_to_wav('same phrase','en-GB-RyanNeural',out_dir=str(tmp_path/'public'))
    private,_=await synth.synth_to_wav('same phrase','en-GB-RyanNeural',out_dir=str(tmp_path/'private'))
    assert Path(public).parent==tmp_path/'public'
    assert Path(private).parent==tmp_path/'private'
    Path(private).unlink()
    assert Path(public).is_file()
