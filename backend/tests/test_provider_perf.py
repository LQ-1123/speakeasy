"""Multi-account benchmarks remain isolated, synthetic and independently capped."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

PERF = Path(__file__).resolve().parents[2] / "scripts" / "perf"
sys.path.insert(0, str(PERF))
import fake_upstreams
import smoke
import seed_bench_data
from bench_server import PoolPeaks


def fake_cfg(**overrides):
    return argparse.Namespace(**{
        "fish_concurrency": 0, "fish_per_account": True, "fish_account_concurrency": 1,
        "fish_base": .03, "fish_per_char": 0, "llm_ttft": 0,
        "llm_tps": 1000, "llm_json_latency": 0, "stt_latency": 0, **overrides,
    })


def test_pool_flags_forward_to_isolated_seed_and_per_account_fake(tmp_path):
    args = smoke.parse_args(["--out-dir", str(tmp_path / "run"), "--pool-fish-accounts", "3",
        "--pool-fish-concurrency", "5", "--pool-llm-accounts", "2", "--pool-llm-concurrency", "10"])
    seed = smoke.seed_arguments(args, tmp_path / "data", "http://127.0.0.1:9000")
    fake = smoke.fake_arguments(args, 9000)
    assert seed[seed.index("--pool-fish-accounts") + 1] == "3"
    assert seed[seed.index("--pool-llm-concurrency") + 1] == "10"
    assert "--fish-per-account" in fake
    assert fake[fake.index("--fish-account-concurrency") + 1] == "5"
    default = smoke.parse_args(["--out-dir", str(tmp_path / "default")])
    assert default.pool_fish_accounts == default.pool_llm_accounts == 0
    assert "--fish-per-account" not in smoke.fake_arguments(default, 9000)


@pytest.mark.parametrize("flag,value", [("--pool-fish-accounts", "-1"), ("--pool-fish-concurrency", "0"), ("--pool-llm-concurrency", "-1")])
def test_invalid_pool_counts_are_rejected(tmp_path, flag, value):
    with pytest.raises(SystemExit):
        smoke.parse_args(["--out-dir", str(tmp_path / "run"), flag, value])


def test_fake_fish_caps_each_authorization_without_a_hidden_global_one():
    async def check():
        app = fake_upstreams.build_app(fake_cfg())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            async def call(key):
                return await client.post("/v1/tts", headers={"Authorization": f"Bearer {key}"}, json={"text": "hello", "format": "wav"})
            responses = await asyncio.gather(call("synthetic-A"), call("synthetic-A"), call("synthetic-B"))
            assert sorted(response.status_code for response in responses) == [200, 200, 429]
            stats = (await client.get("/__stats")).json()["fish"]
            assert stats["peak"] == 2
            assert stats["rejected_429"] == 1
            assert len(stats["accounts"]) == 2
            assert max(item["peak"] for item in stats["accounts"].values()) == 1
            assert "synthetic-A" not in json.dumps(stats)
            assert "synthetic-B" not in json.dumps(stats)
            await client.post("/__reset")
            reset = (await client.get("/__stats")).json()["fish"]
            assert reset["peak"] == reset["rejected_429"] == 0
            assert all(item["peak"] == 0 for item in reset["accounts"].values())
    asyncio.run(check())


def test_seed_fixture_activates_matching_models_in_independent_groups(tmp_path, monkeypatch):
    from app.providers.store import ProviderStore
    from app.v2.db import DB
    monkeypatch.delenv("PROVIDER_POOL_MASTER_KEY", raising=False)
    db = DB(str(tmp_path / "speakeasy_v2.db"))
    db.init()
    try:
        result = seed_bench_data.seed_provider_pool(db, tmp_path, "http://127.0.0.1:9999",
            fish_accounts=3, fish_concurrency=5, llm_accounts=2, llm_concurrency=10)
        store = ProviderStore(db, tmp_path)
        rows = store.list_resources()["items"]
        assert len(rows) == 5 and all(row["state"] == "active" for row in rows)
        fish = [row for row in rows if row["provider"] == "fish"]
        assert {row["model"] for row in fish} == {"s2.1-pro-free"}
        assert len({row["quota_group_id"] for row in fish}) == 3
        assert sum(row["concurrency_limit"] for row in fish) == 15
        assert {row["model"] for row in rows if row["provider"] == "llm"} == {"fake-chat"}
        assert store.provider_enabled("fish") and store.provider_enabled("llm")
        assert "api_key" not in json.dumps(result)
        assert result["synthetic_verification"] is True
        with pytest.raises(ValueError, match="loopback"):
            seed_bench_data.seed_provider_pool(db, tmp_path, "https://example.com", fish_accounts=1)
    finally:
        db.close()


def test_pool_peak_monitor_aggregates_groups_and_resets_without_secrets():
    peaks = PoolPeaks()
    peaks.observe({"active": 9, "providers": {"fish": {"active": 9}}, "resources": [
        {"id": "r1", "provider": "fish", "active": 4, "quota_group_id": "shared"},
        {"id": "r2", "provider": "fish", "active": 5, "quota_group_id": "shared"},
    ]})
    peaks.observe({"active": 0, "providers": {"fish": {"active": 0}}, "resources": []})
    assert peaks.snapshot()["provider_active_peaks"]["fish"] == 9
    assert peaks.snapshot()["quota_group_active_peaks"]["shared"] == 9
    assert peaks.snapshot()["resource_active_peaks"] == {"r1": 4, "r2": 5}
    peaks.reset()
    assert peaks.snapshot()["active_peak"] == 0


def test_seeded_three_account_scheduler_reaches_fifteen_slots(tmp_path, monkeypatch):
    from app.providers.pool import ProviderPool
    from app.providers.store import ProviderStore
    from app.v2.db import DB
    monkeypatch.delenv("PROVIDER_POOL_MASTER_KEY", raising=False)
    db = DB(str(tmp_path / "speakeasy_v2.db"))
    db.init()
    seed_bench_data.seed_provider_pool(db, tmp_path, "http://127.0.0.1:9999", fish_accounts=3)
    async def check():
        pool = ProviderPool(ProviderStore(db, tmp_path))
        await pool.reload()
        entered = []
        release = asyncio.Event()
        async def request():
            async with pool.lease("fish", "s2.1-pro-free", deadline=asyncio.get_running_loop().time() + 2) as lease:
                entered.append(lease.resource_id)
                await release.wait()
                lease.success()
        tasks = [asyncio.create_task(request()) for _ in range(16)]
        for _ in range(100):
            if len(entered) == 15:
                break
            await asyncio.sleep(.001)
        try:
            assert len(entered) == 15
            assert len(set(entered)) == 3
            assert all(entered.count(rid) == 5 for rid in set(entered))
            assert pool.stats()["waiting"] == 1
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await pool.aclose()
    try:
        asyncio.run(check())
    finally:
        db.close()
