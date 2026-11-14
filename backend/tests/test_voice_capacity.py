"""Offline resource bounds: queues, cancelled provider calls and hostile uploads."""
from __future__ import annotations

import asyncio
import io
import wave
import subprocess

import httpx
import numpy as np
import pytest

from app.v2.audio_store import AudioStore
from app.voice import audio_utils, fish_tts, sensevoice


def wav_bytes(seconds: float, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sr)
        output.writeframes(b"\0\0" * int(seconds * sr))
    return buf.getvalue()


async def test_fish_queue_rejects_overflow_and_removes_cancelled_waiters(monkeypatch):
    monkeypatch.setenv("SE_FISH_CONCURRENCY", "1")
    monkeypatch.setenv("SE_FISH_QUEUE_LIMIT", "2")
    limiter = fish_tts._Limiter()
    await limiter.acquire(1, 5)
    waiters = [asyncio.create_task(limiter.acquire(1, 5)) for _ in range(2)]
    await asyncio.sleep(0)
    try:
        with pytest.raises(fish_tts.FishTTSError, match="queue|排队"):
            await asyncio.wait_for(limiter.acquire(1, 5), 0.1)
        waiters[0].cancel()
        await asyncio.gather(waiters[0], return_exceptions=True)
        assert limiter.waiting == 1
        assert sum(len(queue) for queue in limiter._waiting.values()) == 1
    finally:
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        limiter.release()
    assert limiter.active == limiter.waiting == 0


async def test_fish_cancel_after_grant_returns_slot(monkeypatch):
    monkeypatch.setenv("SE_FISH_CONCURRENCY", "1")
    limiter = fish_tts._Limiter()
    await limiter.acquire(1, 1)
    task = asyncio.create_task(limiter.acquire(1, 1))
    await asyncio.sleep(0)
    limiter.release()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert limiter.active == limiter.waiting == 0


async def test_fish_priority_preserves_fifo_and_timeout_frees_queue(monkeypatch):
    monkeypatch.setenv("SE_FISH_CONCURRENCY", "1")
    limiter = fish_tts._Limiter()
    await limiter.acquire(1, 5)
    order = []

    async def run(label, level):
        await limiter.acquire(level, 1)
        order.append(label)
        limiter.release()

    tasks = [asyncio.create_task(run(label, level)) for label, level in
             [("bg", 2), ("chat1", 1), ("chat2", 1), ("call", 0)]]
    await asyncio.sleep(0)
    with pytest.raises(fish_tts.FishTTSError):
        await limiter.acquire(1, 0.001)
    assert limiter.waiting == 4
    limiter.release()
    await asyncio.gather(*tasks)
    assert order == ["call", "chat1", "chat2", "bg"]
    assert limiter.active == limiter.waiting == 0


def test_upload_rejects_oversize_before_creating_tempfile(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_AUDIO_MAX_BYTES", "16")
    store = AudioStore(str(tmp_path))
    monkeypatch.setattr("app.v2.audio_store.read_audio_file", lambda *a, **k: pytest.fail("oversized upload decoded"))
    with pytest.raises(ValueError, match="大小|bytes|size"):
        store.decode_upload(b"a" * 17, "clip.webm")
    assert list((tmp_path / "audio").iterdir()) == []


def test_upload_rejects_long_wav_without_reading_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_AUDIO_MAX_DURATION_S", "1")
    source = wav_bytes(1.1)
    original = wave.Wave_read.readframes

    def checked_read(self, count):
        assert self.getnframes() <= self.getframerate(), "long WAV fully decoded"
        return original(self, count)

    monkeypatch.setattr(wave.Wave_read, "readframes", checked_read)
    store = AudioStore(str(tmp_path))
    with pytest.raises(ValueError, match="duration|时长"):
        store.decode_upload(source, "long.wav")
    assert list((tmp_path / "audio").iterdir()) == []


def test_bounded_ffmpeg_decoding_caps_duration_output_threads_and_timeout(tmp_path, monkeypatch):
    source = tmp_path / "recording.webm"
    source.write_bytes(b"encoded")
    monkeypatch.setattr(audio_utils, "_find_ffmpeg", lambda: "ffmpeg")
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        kwargs["stdout"].write(np.zeros(1600, dtype="<f4").tobytes())
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(audio_utils.subprocess, "run", run)
    audio = audio_utils.read_audio_file(str(source), max_duration_s=1, timeout_s=2)
    assert audio.size == 1600
    cmd = seen["command"]
    assert cmd[cmd.index("-threads") + 1] == "1"
    assert 1 < float(cmd[cmd.index("-t") + 1]) < 1.01
    assert int(cmd[cmd.index("-fs") + 1]) <= 16000 * 4 + 8
    assert seen["kwargs"]["timeout"] == 2


async def test_sensevoice_reuses_client_and_bounds_cancellation(monkeypatch):
    monkeypatch.setenv("SE_STT_CONCURRENCY", "1")
    monkeypatch.setenv("SE_STT_QUEUE_LIMIT", "1")
    started, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"text": "hello"})
        finally:
            active -= 1

    client = sensevoice.SiliconFlowSTT(base_url="https://offline.invalid", api_key="test",
                                     model="test", transport=httpx.MockTransport(handler))
    first_client = client._client()
    assert client._client() is first_client
    first = asyncio.create_task(client.transcribe(b"\0\0" * 1600))
    await started.wait()
    queued = asyncio.create_task(client.transcribe(b"\0\0" * 1600))
    await asyncio.sleep(0)
    try:
        with pytest.raises(sensevoice.SenseVoiceError, match="queue|排队"):
            await asyncio.wait_for(client.transcribe(b"\0\0" * 1600), 0.1)
        queued.cancel()
        first.cancel()
        await asyncio.gather(queued, first, return_exceptions=True)
        release.set()
        assert (await client.transcribe(b"\0\0" * 1600)).text == "hello"
        assert peak == 1
    finally:
        release.set()
        for task in (queued, first):
            task.cancel()
        await asyncio.gather(queued, first, return_exceptions=True)
        await client.aclose()
    assert first_client.is_closed


async def test_fish_retry_respects_total_deadline_and_releases_slot(monkeypatch):
    monkeypatch.setenv("SE_FISH_TOTAL_TIMEOUT_S", "0.03")
    state = fish_tts._state()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(fish_tts.FishTTSError, match="deadline|超时"):
            await asyncio.wait_for(fish_tts.synthesize("hello", api_key="offline"), 0.2)
        assert asyncio.get_running_loop().time() - started < 0.15
        assert calls == 1
        assert state.limiter.active == state.limiter.waiting == 0
    finally:
        await fish_tts.aclose()


async def test_fish_thousands_of_cancelled_waiters_do_not_accumulate(monkeypatch):
    monkeypatch.setenv("SE_FISH_CONCURRENCY", "1")
    monkeypatch.setenv("SE_FISH_QUEUE_LIMIT", "20")
    limiter = fish_tts._Limiter()
    await limiter.acquire(1, 1)
    for _ in range(100):
        tasks = [asyncio.create_task(limiter.acquire(level % 3, 5)) for level in range(20)]
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert limiter.waiting == 0
        assert not any(limiter._waiting.values())
    limiter.release()
    assert limiter.active == 0


def test_mp3_duration_uses_frame_samples_and_excludes_id3_tag():
    # 128kbps MPEG1 Layer III, 44.1kHz: 417-byte frames, 1152 samples each.
    frame = b"\xff\xfb\x90\0" + bytes(413)
    tag = b"ID3\x04\0\0\0\0\x20\0" + bytes(4096)
    expected = round(20 * 1152 / 44100 * 1000)
    assert fish_tts.estimate_mp3_duration_ms(tag + frame * 20) == expected


def test_pcm_conversion_does_not_mutate_readonly_source():
    source = np.array([float("nan"), -2, -0.5, 0.5, 2], dtype=np.float32)
    source.flags.writeable = False
    actual = np.frombuffer(audio_utils.float32_to_pcm16(source), dtype="<i2")
    np.testing.assert_array_equal(actual, [0, -32767, -16384, 16384, 32767])
    assert np.isnan(source[0])
    assert source[1] == -2


def test_compressed_upload_duration_limit_with_real_ffmpeg(tmp_path):
    ffmpeg = audio_utils._find_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg is optional in the offline environment")
    for duration, rejected in [(0.2, False), (1.1, True)]:
        source = tmp_path / f"source_{duration}.wav"
        compressed = tmp_path / f"source_{duration}.flac"
        source.write_bytes(wav_bytes(duration))
        subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-threads", "1",
                        "-i", str(source), "-threads", "1", str(compressed)],
                       check=True, capture_output=True, timeout=5)
        if rejected:
            with pytest.raises(audio_utils.AudioLimitError):
                audio_utils.read_audio_file(str(compressed), max_duration_s=1, timeout_s=5)
        else:
            audio = audio_utils.read_audio_file(str(compressed), max_duration_s=1, timeout_s=5)
            assert len(audio) == 3200


async def test_fish_queue_overflow_never_calls_http(monkeypatch):
    monkeypatch.setenv("SE_FISH_CONCURRENCY", "1")
    monkeypatch.setenv("SE_FISH_QUEUE_LIMIT", "0")
    state = fish_tts._state()
    await state.limiter.acquire(1, 1)
    monkeypatch.setattr(fish_tts, "_client", lambda state: pytest.fail("overflow reached HTTP"))
    try:
        with pytest.raises(fish_tts.FishTTSError, match="queue full"):
            await fish_tts.synthesize("hello", api_key="offline")
    finally:
        state.limiter.release()


async def test_sensevoice_shared_production_pool_closes_on_shutdown():
    one = sensevoice.SiliconFlowSTT(base_url="https://offline.invalid", api_key="one", model="one")
    two = sensevoice.SiliconFlowSTT(base_url="https://offline.invalid", api_key="two", model="two")
    # Exercise the production shared-pool path with an offline transport.
    sensevoice._pool().transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"text": "ok"}))
    shared = one._client()
    try:
        assert two._client() is shared
    finally:
        await sensevoice.aclose()
    assert shared.is_closed


def test_mp3_duration_handles_variable_bitrate_and_mpeg2():
    # 128 and 192kbps MPEG1 frames share 1152 samples despite different sizes.
    frames = [(b"\xff\xfb\x90\0" + bytes(413)),
              (b"\xff\xfb\xb0\0" + bytes(622))]
    assert fish_tts.estimate_mp3_duration_ms(b"".join(frames) * 10) == round(20 * 1152 / 44100 * 1000)
    # MPEG2, 64kbps, 22.05kHz: 576 samples, 208 bytes per frame.
    frame2 = b"\xff\xf3\x80\0" + bytes(204)
    assert fish_tts.estimate_mp3_duration_ms(frame2 * 30) == round(30 * 576 / 22050 * 1000)


def test_exotic_wav_sample_rate_uses_bounded_subprocess_before_readframes(tmp_path, monkeypatch):
    source = tmp_path / "exotic.wav"
    source.write_bytes(wav_bytes(0.01, sr=1000000))
    monkeypatch.setattr(wave.Wave_read, "readframes", lambda *args: pytest.fail("exotic WAV decoded in process"))
    seen = []

    def bounded(*args, **kwargs):
        seen.append(kwargs)
        return np.zeros(160, dtype=np.float32), 16000

    monkeypatch.setattr(audio_utils, "_read_via_ffmpeg", bounded)
    assert len(audio_utils.read_audio_file(str(source), max_duration_s=1, timeout_s=2)) == 160
    assert seen == [{"max_duration_s": 1, "timeout_s": 2}]


async def test_sensevoice_rejects_long_pcm_before_provider(monkeypatch):
    monkeypatch.setenv("SE_AUDIO_MAX_DURATION_S", "0.1")
    client = sensevoice.SiliconFlowSTT(base_url="https://offline.invalid", api_key="test", model="test",
                                     transport=httpx.MockTransport(lambda _: pytest.fail("long PCM sent")))
    with pytest.raises(sensevoice.SenseVoiceError, match="duration|时长"):
        await client.transcribe(b"\0\0" * 1601)


@pytest.mark.parametrize("failure", ["busy", "stale", "bitrate"])
async def test_fish_existing_retry_paths_still_produce_audio(failure, monkeypatch):
    monkeypatch.setattr(fish_tts, "_bitrate_rejected", False)
    state = fish_tts._state()
    seen = []

    async def handler(request):
        seen.append(request.content)
        if failure == "busy" and len(seen) <= 2:
            return httpx.Response(429)
        if failure == "stale" and len(seen) == 1:
            raise httpx.RemoteProtocolError("stale keepalive")
        if failure == "bitrate" and len(seen) == 1:
            return httpx.Response(422)
        return httpx.Response(200, content=b"audio" * 200)

    state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await fish_tts.synthesize("hello", api_key="offline", audio_format="mp3")
        assert result == b"audio" * 200
        assert len(seen) == (3 if failure == "busy" else 2)
        assert state.limiter.active == state.limiter.waiting == 0
        if failure == "bitrate":
            assert b'"mp3_bitrate"' in seen[0]
            assert b'"mp3_bitrate"' not in seen[1]
    finally:
        await fish_tts.aclose()


async def test_fish_model_catalog_compatibility_uses_shared_client_and_cache():
    state = fish_tts._state()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"total": 1, "items": [{"_id": "voice1", "title": "Voice",
                                    "languages": ["en"], "tags": ["female"], "cover_image": "cover"}]})

    state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await fish_tts.list_models(api_key="offline-catalog", base_url="https://offline.invalid")
        second = await fish_tts.list_models(api_key="offline-catalog", base_url="https://offline.invalid")
        assert first == second == [{"id": "voice1", "title": "Voice", "description": "",
                                    "languages": ["en"], "tags": ["female"], "cover": "cover", "total": 1}]
        assert len(seen) == 1
        assert seen[0].url.params["page_size"] == "100"
        assert state.limiter.active == state.limiter.waiting == 0
    finally:
        await fish_tts.aclose()


async def test_fish_pick_voice_preserves_gender_then_language_fallback(monkeypatch):
    async def catalog(**kwargs):
        return [{"id": "used", "languages": ["en"], "tags": ["female"]},
                {"id": "female-ja", "languages": ["ja"], "tags": ["female"]},
                {"id": "male-en", "languages": ["en"], "tags": ["male"]}]

    monkeypatch.setattr(fish_tts, "list_models", catalog)
    assert await fish_tts.pick_voice_for(language="en", gender="female", used={"used"}) == "female-ja"


async def test_fish_pick_voice_catalog_failure_returns_empty(monkeypatch):
    async def catalog(**kwargs):
        raise fish_tts.FishTTSError("offline")

    monkeypatch.setattr(fish_tts, "list_models", catalog)
    assert await fish_tts.pick_voice_for(api_key="offline") == ""
