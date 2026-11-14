"""Capacity tooling uses local fake requests, never real providers."""
import asyncio
import argparse
import io
from pathlib import Path
import sys
import wave
import pytest

import httpx

PERF = Path(__file__).resolve().parents[2] / "scripts" / "perf"
sys.path.insert(0, str(PERF))
import loadgen
import fake_upstreams
import smoke
from perf_utils import isolated_env, require_loopback


def cfg(**kwargs):
    return argparse.Namespace(base="http://test", no_keepalive=False,
        audio_deadline=0.1, poll_initial=0.001, **kwargs)


def test_fake_tts_returns_decodable_wav_and_rejects_unsupported_format():
    async def check():
        app = fake_upstreams.build_app(argparse.Namespace(fish_concurrency=1,
            fish_base=0, fish_per_char=0, llm_ttft=0, llm_tps=1000,
            llm_json_latency=0, stt_latency=0))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/v1/tts", json={"text": "Hello", "format": "wav"})
            assert response.headers["content-type"] == "audio/wav"
            with wave.open(io.BytesIO(response.content), "rb") as audio:
                assert audio.getnframes() > 0 and audio.getframerate() == 24000
            invalid = await client.post("/v1/tts", json={"text": "Hello", "format": "invalid"})
            assert invalid.status_code == 422
    asyncio.run(check())


def test_text_turn_fetches_messages_then_polls_audio_and_records_playable():
    async def check():
        requests = []
        polls = 0
        audio = loadgen.make_wav(.01)
        def handler(request):
            nonlocal polls
            requests.append((request.method, request.url.path))
            if request.method == "POST" and request.url.path.endswith("/text"):
                return httpx.Response(200, json={"friend_messages": [{"id": "reply"}]})
            if request.url.path.endswith("/messages"):
                polls += 1
                return httpx.Response(200, json={"messages": [{"id": "reply", "sender": "friend",
                    "msg_type": "voice", "audio_status": "queued" if polls == 1 else "ready",
                    "audio_url": "" if polls == 1 else "/audio.wav"}]})
            if request.url.path == "/audio.wav":
                return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})
            return httpx.Response(200, json={})
        metrics = loadgen.Metrics()
        metrics.recording = True
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
            vu = loadgen.VUser(cfg(), {"user_id": "user", "token": "fake", "friends": ["friend"]}, client, metrics, {})
            await vu.text_turn()
        assert requests[0] == ("POST", "/api/v2/chat/friend/text")
        assert requests[1] == ("GET", "/api/v2/friends/friend/messages")
        assert polls == 2
        assert metrics.count["turns.offered"] == metrics.count["turns.completed"] == 1
        assert metrics.count["turns.playable_audio"] == 1
        assert metrics.latency["time_to_playable"]
    asyncio.run(check())


def test_rejected_request_and_invalid_audio_are_not_successes():
    async def check():
        for status, body, expected in [(429, {}, "turns.rejected"),
                (200, {"friend_messages": [{"id": "reply", "audio_url": "/invalid.mp3"}]}, "turns.missing_audio")]:
            def handler(request):
                if request.url.path.endswith("/text"):
                    return httpx.Response(status, json=body)
                if request.url.path.endswith("/messages"):
                    return httpx.Response(200, json={"messages": body.get("friend_messages", [])})
                if request.url.path.endswith(".mp3"):
                    return httpx.Response(200, content=b"\xff\xfb\x90\x00" + bytes(2044))
                return httpx.Response(200, json={})
            metrics = loadgen.Metrics()
            metrics.recording = True
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
                vu = loadgen.VUser(cfg(), {"user_id": "u", "token": "fake", "friends": ["f"]}, client, metrics, {})
                await vu.text_turn()
            assert metrics.count[expected] == 1
            assert metrics.count["turns.playable_audio"] == 0
    asyncio.run(check())


def test_fixed_arrivals_do_not_wait_for_slow_work_and_bound_tasks():
    async def check():
        metrics = loadgen.Metrics()
        metrics.recording = True
        active = peak = 0
        async def action(_index):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.2)
            active -= 1
        await loadgen.schedule_arrivals(action, metrics, duration=.1, rate=100,
                                       burst=0, max_inflight=2, drain_timeout=1)
        assert metrics.count["arrivals.offered"] == 10
        assert metrics.count["arrivals.started"] == 2
        assert metrics.count["arrivals.client_dropped"] == 8
        assert peak == 2
    asyncio.run(check())


def test_burst_offers_every_user_at_once_with_bounded_tasks():
    async def check():
        metrics = loadgen.Metrics()
        metrics.recording = True
        await loadgen.schedule_arrivals(lambda _: asyncio.sleep(.01), metrics,
            duration=.01, rate=0, burst=20, max_inflight=3, drain_timeout=1)
        assert metrics.count["arrivals.offered"] == 20
        assert metrics.count["arrivals.client_dropped"] == 17
    asyncio.run(check())


def test_environment_uses_no_inherited_keys_or_external_endpoints(tmp_path):
    env = isolated_env(str(tmp_path), "http://127.0.0.1:9000", {
        "PATH": "runtime", "OPENAI_API_KEY": "real", "JPUSH_APP_KEY": "real",
        "STT_SILICONFLOW_BASE_URL": "https://external.invalid", "SE_SMTP_PASSWORD": "real"})
    assert "real" not in env.values()
    assert env["PERF_EXTERNAL_PROVIDER_BUDGET"] == "0"
    assert env["STT_SILICONFLOW_BASE_URL"] == "http://127.0.0.1:9000/v1"
    assert env["SE_LOAD_DOTENV"] == "0"
    with pytest.raises(ValueError, match="loopback"):
        require_loopback("https://192.0.2.1")


def test_event_refresh_targets_only_active_thread_and_lightweight_friends():
    async def check():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, json={"messages": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
            vu = loadgen.VUser(cfg(), {"user_id": "u", "token": "fake", "friends": ["f"]}, client, loadgen.Metrics(), {})
            vu.current_friend = "f"
            await vu._refresh_after_event({"type": "audio_ready", "friend_id": "f"})
            assert paths == ["/api/v2/friends/f/messages"]
            paths.clear()
            await vu._refresh_after_event({"type": "message", "friend_id": "other"})
            assert paths == ["/api/v2/friends"]
    asyncio.run(check())


def test_fake_llm_injected_429_and_503_are_visible():
    async def check():
        app = fake_upstreams.build_app(argparse.Namespace(llm_429_every=2,
            llm_error_every=3, llm_json_latency=0))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            statuses = [(await client.post("/v1/chat/completions", json={})).status_code for _ in range(3)]
        assert statuses == [200, 429, 503]
    asyncio.run(check())


def test_smoke_forwards_latency_controls_and_maintenance(tmp_path):
    args = smoke.parse_args(["--out-dir", str(tmp_path / "unused"), "--fish-base", "3",
        "--fish-per-char", "0.02", "--llm-ttft", "0.4", "--maintenance"])
    fake = smoke.fake_arguments(args, 9000)
    assert fake[fake.index("--fish-base") + 1] == "3.0"
    assert fake[fake.index("--fish-per-char") + 1] == "0.02"
    assert fake[fake.index("--llm-ttft") + 1] == "0.4"
    assert "--no-maintenance" not in smoke.backend_arguments(args, tmp_path, 8000, "http://127.0.0.1:9000")
    default = smoke.parse_args(["--out-dir", str(tmp_path / "default")])
    assert default.fish_base == default.llm_ttft == 0.01
    assert default.fish_per_char == 0
    assert "--no-maintenance" in smoke.backend_arguments(default, tmp_path, 8000, "http://127.0.0.1:9000")


def test_hundred_user_burst_counts_every_offer_even_when_generator_is_bounded():
    async def check():
        metrics = loadgen.Metrics()
        metrics.recording = True
        await loadgen.schedule_arrivals(lambda _: asyncio.sleep(.01), metrics,
            duration=.01, rate=0, burst=100, max_inflight=100, drain_timeout=1)
        assert metrics.count["arrivals.offered"] == 100
        assert metrics.count["arrivals.started"] == 100
        assert metrics.count["arrivals.client_dropped"] == 0
    asyncio.run(check())
