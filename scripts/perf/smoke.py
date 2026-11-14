"""Run isolated zero-external-budget fake-provider capacity smoke on Windows/Linux.

python scripts/perf/smoke.py --out-dir .tmp/perf-smoke --users 4 --duration 4 --rate 1
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

PERF = Path(__file__).resolve().parent
sys.path.insert(0, str(PERF))
fallback = PERF.parents[1] / ".venv" / "Lib" / "site-packages"
if fallback.is_dir():
    sys.path.append(str(fallback))
import httpx
from perf_utils import isolated_env, resource_snapshot, source_provenance


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_ready(url, process, timeout=45):
    started = time.monotonic()
    async with httpx.AsyncClient(trust_env=False) as client:
        while time.monotonic() - started < timeout:
            if process.poll() is not None:
                raise RuntimeError(f"Benchmark process exited with {process.returncode}; inspect its log")
            try:
                response = await client.get(url, timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.15)
    raise TimeoutError(f"Local benchmark server failed to start within {timeout}s")


def stop_owned(process):
    if process.poll() is not None:
        return
    children = []
    try:
        import psutil
        children = psutil.Process(process.pid).children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                pass
    except (ImportError, ProcessLookupError):
        pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if children:
        _gone, alive = psutil.wait_procs(children, timeout=2)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass


def fake_arguments(args, port):
    return ["--port", str(port), "--llm-ttft", str(args.llm_ttft),
            "--llm-tps", "2000", "--llm-json-latency", "0.01",
            "--fish-base", str(args.fish_base), "--fish-per-char", str(args.fish_per_char),
            "--fish-concurrency", str(args.fake_concurrency),
            *(["--fish-per-account", "--fish-account-concurrency", str(args.pool_fish_concurrency)]
              if args.pool_fish_accounts else [])]


def seed_arguments(args, data, fake):
    return ["--data-dir", str(data), "--users", str(args.users), "--friends", "2",
            "--history", str(args.history), "--fake-upstream", fake,
            "--pool-fish-accounts", str(args.pool_fish_accounts),
            "--pool-fish-concurrency", str(args.pool_fish_concurrency),
            "--pool-llm-accounts", str(args.pool_llm_accounts),
            "--pool-llm-concurrency", str(args.pool_llm_concurrency)]


def backend_arguments(args, data, port, fake):
    return ["--data-dir", str(data), "--port", str(port), "--fake-upstream", fake,
            *([] if args.maintenance else ["--no-maintenance"])]


async def run(args):
    output = Path(args.out_dir).resolve()
    # Never clear/reseed an existing directory containing unknown user data.
    output.mkdir(parents=True, exist_ok=False)
    data = output / "data"
    data.mkdir()
    fake_port, app_port = unused_port(), unused_port()
    fake = f"http://127.0.0.1:{fake_port}"
    base = f"http://127.0.0.1:{app_port}"
    env = isolated_env(str(data), fake)
    entry = str(PERF / "runtime_entry.py")
    processes, logs = [], []

    def start(tool, arguments, log_name):
        log = (output / log_name).open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen([sys.executable, entry, str(PERF / tool), *arguments],
            cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        return process

    resource_samples = []
    sampler = None
    try:
        fake_process = start("fake_upstreams.py", fake_arguments(args, fake_port), "fake.log")
        await wait_ready(fake + "/__stats", fake_process)
        seed = start("seed_bench_data.py", seed_arguments(args, data, fake), "seed.log")
        while seed.poll() is None:
            await asyncio.sleep(.1)
        if seed.returncode:
            raise RuntimeError("Seeding failed; inspect seed.log")
        server = start("bench_server.py", backend_arguments(args, data, app_port, fake), "server.log")
        await wait_ready(base + "/__perf/stats", server)

        async def sample():
            while True:
                snapshot = await asyncio.to_thread(resource_snapshot, [server.pid, fake_process.pid])
                snapshot["elapsed_s"] = time.monotonic() - started
                resource_samples.append(snapshot)
                await asyncio.sleep(.2)

        started = time.monotonic()
        sampler = asyncio.create_task(sample())
        from loadgen import run as load
        config = argparse.Namespace(base=base, fake=fake, users_file=str(data / "users.json"),
            users=args.users, calls=0, scenario="chat", mix="chat=1", no_keepalive=False,
            no_events=args.no_events, ramp=0, warmup=.25, duration=args.duration, think=30,
            mode=args.mode, rate=args.rate, burst=args.users, max_inflight=args.max_inflight,
            drain_timeout=35, audio_deadline=30, poll_initial=2, profile=False,
            label="isolated-fake-only-smoke", playback_cap=0)
        result = await load(config)
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        sampler = None
        result["resources"] = {"scope": "owned backend + fake upstream process trees; load generator excluded",
            "samples": resource_samples,
            "peak_process_tree_rss_bytes": max((s["process_tree"].get("rss_bytes", 0) for s in resource_samples), default=0)}
        if len(resource_samples) >= 2:
            first, last = resource_samples[0], resource_samples[-1]
            seconds = last["elapsed_s"] - first["elapsed_s"]
            result["resources"]["sampled_duration_s"] = seconds
            for scope, field in (("process_tree", "cpu_s"), ("host", "busy_cpu_s"), ("cgroup", "cpu_s")):
                if first[scope].get("available") and last[scope].get("available"):
                    delta = last[scope][field] - first[scope][field]
                    result["resources"][scope + "_cpu_s"] = delta
                    result["resources"][scope + "_cores_used"] = delta / seconds if seconds else None
        result["safety"] = {"external_provider_budget": 0, "loopback_only_socket_guard": True,
            "env_file_disabled": True, "data_dir": str(data), "owned_pids": [p.pid for p in processes]}
        result["smoke_provenance_end"] = source_provenance()
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"result": str(output / "result.json"), "counts": result["counts"],
                          "rates": result["rates"]}, ensure_ascii=False, indent=2))
        counts = result["counts"]
        offered = counts.get("arrivals.offered", 0)
        return 0 if offered and counts.get("turns.audio_within_deadline", 0) == offered else 1
    finally:
        if sampler:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        for process in reversed(processes):
            stop_owned(process)
        for log in logs:
            log.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", required=True, help="New, nonexistent output directory")
    parser.add_argument("--users", type=int, default=4)
    parser.add_argument("--history", type=int, default=20)
    parser.add_argument("--duration", type=float, default=4)
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--mode", choices=("arrival", "burst"), default="arrival")
    parser.add_argument("--max-inflight", type=int, default=100)
    parser.add_argument("--fake-concurrency", type=int, default=20)
    parser.add_argument("--pool-fish-accounts", type=int, default=0,
                        help="Synthetic verified Fish accounts in independent quota groups (0=legacy)")
    parser.add_argument("--pool-fish-concurrency", type=int, default=5)
    parser.add_argument("--pool-llm-accounts", type=int, default=0,
                        help="Synthetic verified LLM accounts in independent quota groups (0=legacy)")
    parser.add_argument("--pool-llm-concurrency", type=int, default=10)
    parser.add_argument("--fish-base", type=float, default=.01,
                        help="Fake TTS fixed processing delay in seconds")
    parser.add_argument("--fish-per-char", type=float, default=0,
                        help="Fake TTS additional seconds per input character")
    parser.add_argument("--llm-ttft", type=float, default=.01,
                        help="Fake streaming LLM first-token delay in seconds")
    parser.add_argument("--maintenance", action="store_true",
                        help="Include normal background maintenance (still loopback-only)")
    parser.add_argument("--no-events", action="store_true")
    args = parser.parse_args(argv)
    if args.users < 1 or args.duration <= 0 or args.rate <= 0 or args.max_inflight < 1:
        parser.error("users, duration, rate and max-inflight must be positive")
    if min(args.fish_base, args.fish_per_char, args.llm_ttft) < 0:
        parser.error("fake upstream delays must not be negative")
    if min(args.pool_fish_accounts, args.pool_llm_accounts, args.fake_concurrency) < 0:
        parser.error("pool account counts and fake-concurrency must not be negative")
    if min(args.pool_fish_concurrency, args.pool_llm_concurrency) < 1:
        parser.error("pool account concurrency must be positive")
    return args


def main():
    args = parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
