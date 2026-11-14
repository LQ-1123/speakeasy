"""压测专用启动器：用真实的 app.main:app，只把上游换成假的，并暴露进程指标。

与线上的差别只有三处（都在本文件里，业务代码一行不改）：
    1. LLM / Fish / STT / Deepgram 的地址指向 fake_upstreams.py；
    2. 多出 /__perf/* 观测端点（CPU 秒、RSS、线程数、事件循环卡顿）；
    3. 可选 --no-maintenance：关掉后台世界生成，单独量请求路径的成本。

    PYTHONPATH=backend python scripts/perf/bench_server.py --data-dir .tmp/perf/data --port 8011
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import os
import pstats
import sys
import threading
import time
from perf_utils import isolated_env, resource_snapshot, require_loopback, install_loopback_guard

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def _rss_mb() -> dict:
    """当前/峰值常驻内存（MB）。Linux 读 /proc，Windows 走 psapi。"""
    try:
        if sys.platform.startswith("linux"):
            out = {}
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(("VmRSS:", "VmHWM:")):
                        key, value = line.split(":", 1)
                        out["rss_mb" if key == "VmRSS" else "peak_rss_mb"] = round(int(value.split()[0]) / 1024, 1)
            return out
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return {"rss_mb": round(counters.WorkingSetSize / 1048576, 1),
                        "peak_rss_mb": round(counters.PeakWorkingSetSize / 1048576, 1)}
    except Exception:  # noqa: BLE001 - 指标拿不到不影响压测
        pass
    return {}


class PoolPeaks:
    """Sample public scheduler counters, retaining high-water evidence after drain."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.active_peak = 0
        self.providers = {}
        self.resources = {}
        self.groups = {}

    def observe(self, stats):
        self.active_peak = max(self.active_peak, stats.get("active", 0))
        providers = stats.get("providers", {})
        if isinstance(providers, list):
            providers = {item["provider"]: item for item in providers}
        for name, item in providers.items():
            self.providers[name] = max(self.providers.get(name, 0), item.get("active", 0))
        groups = {}
        for item in stats.get("resources", []):
            rid, active = item.get("id", item.get("resource_id")), item.get("active", 0)
            if rid:
                self.resources[rid] = max(self.resources.get(rid, 0), active)
            group = item.get("quota_group_id")
            if group:
                groups[group] = groups.get(group, 0) + active
        for group, active in groups.items():
            self.groups[group] = max(self.groups.get(group, 0), active)

    def snapshot(self):
        return {"active_peak": self.active_peak, "provider_active_peaks": dict(self.providers),
                "resource_active_peaks": dict(self.resources), "quota_group_active_peaks": dict(self.groups),
                "sampling_interval_s": .02}


class LagMonitor:
    """事件循环卡顿：sleep(interval) 实际多睡了多久。卡顿 = 所有在线用户同时变慢。"""

    def __init__(self, interval: float = 0.02, on_sample=None) -> None:
        self.interval = interval
        self.samples: list[float] = []
        self.task: asyncio.Task | None = None
        self.on_sample = on_sample

    def ensure_started(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="perf-lag-monitor")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(self.interval)
            if self.on_sample:
                self.on_sample()
            self.samples.append(max(0.0, (loop.time() - start - self.interval) * 1000.0))
            if len(self.samples) > 200_000:
                del self.samples[:100_000]

    def snapshot(self) -> dict:
        data = sorted(self.samples)
        if not data:
            return {"samples": 0}
        pick = lambda q: round(data[min(len(data) - 1, int(len(data) * q))], 2)  # noqa: E731
        return {"samples": len(data), "mean_ms": round(sum(data) / len(data), 2),
                "p50_ms": pick(0.5), "p99_ms": pick(0.99), "max_ms": round(data[-1], 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--fake-upstream", default="http://127.0.0.1:9100")
    parser.add_argument("--no-maintenance", action="store_true", help="不跑后台世界生成任务")
    parser.add_argument("--max-calls", type=int, default=0, help="覆盖 SE_MAX_ACTIVE_CALLS（0=不改）")
    parser.add_argument("--profile", default="", help="对事件循环线程开 cProfile，结果写到该文件")
    parser.add_argument("--threadpool-workers", type=int, default=0,
                        help="Limit default threadpool workers; this does not emulate CPU count or RAM")
    args = parser.parse_args()
    install_loopback_guard()

    fake = require_loopback(args.fake_upstream)
    inherited_limits = {key: value for key, value in os.environ.items()
                        if key.startswith(("SE_CHAT_", "SE_LLM_DAILY_", "SE_LLM_MONTHLY_"))}
    clean = isolated_env(args.data_dir, fake)
    clean.update(inherited_limits)
    os.environ.clear()
    os.environ.update(clean)
    env = os.environ
    env["SE_LOAD_DOTENV"] = "0"
    env["DATA_DIR"] = os.path.abspath(args.data_dir)
    env["LLM_BASE_URL"] = fake + "/v1"
    env["LLM_API_KEY"] = "perf-fake-key"
    env["LLM_MODEL"] = "fake-chat"
    env["STT_PROVIDER"] = "siliconflow"
    env["STT_SILICONFLOW_API_KEY"] = "perf-fake-key"
    env["STT_SILICONFLOW_BASE_URL"] = fake + "/v1"
    env["DEEPGRAM_API_KEY"] = "perf-fake-deepgram-key"
    env.setdefault("SE_EMAIL_CODE_AUTH", "1")
    env.setdefault("SE_REQUIRE_AUTH", "1")
    env.setdefault("SE_MEDIA_ENABLED", "0")  # 不去抓开放图库
    env.setdefault("SE_MEDIA_SECRET", "perf-media-secret-0123456789abcdef")
    if args.max_calls > 0:
        env["SE_MAX_ACTIVE_CALLS"] = str(args.max_calls)

    import uvicorn
    from fastapi.routing import APIRoute

    from app.voice import deepgram_stream
    from app import config

    # SE_LOAD_DOTENV disables the explicit loader; BaseSettings env_file must
    # also be disabled to prevent reading workspace .env keys during a smoke.
    config.Settings.model_config["env_file"] = None
    config.get_settings.cache_clear()

    deepgram_stream.DEFAULT_WS = fake.replace("http://", "ws://").replace("https://", "wss://") + "/v1/listen"

    if args.no_maintenance:
        import app.maintenance as maintenance

        async def _idle(*_args, **_kwargs) -> None:
            await asyncio.Event().wait()

        maintenance.maintenance_loop = _idle

    from app.main import app

    pool_peaks = PoolPeaks()

    def pool_stats():
        pool = getattr(app.state.v2, "provider_pool", None)
        return pool.stats() if pool is not None else {}

    lag = LagMonitor(on_sample=lambda: pool_peaks.observe(pool_stats()))
    marks = {"cpu": time.process_time(), "wall": time.time()}
    profiler = cProfile.Profile() if args.profile else None

    async def perf_stats() -> dict:
        lag.ensure_started()
        current_pool = pool_stats()
        pool_peaks.observe(current_pool)
        return {"pid": os.getpid(), "cpu_s": round(time.process_time(), 4), "wall_s": round(time.time(), 3),
                "threads": threading.active_count(), "tasks": len(asyncio.all_tasks()),
                "loop": type(asyncio.get_running_loop()).__module__, "lag": lag.snapshot(), **_rss_mb(),
                "resources": resource_snapshot([os.getpid()]),
                "provider_pool": {"current": current_pool, "peaks": pool_peaks.snapshot()},
                "configuration": {"fake_only": True, "external_provider_budget": 0,
                    "threadpool_workers": args.threadpool_workers, "cpu_emulation": False,
                    "no_maintenance": args.no_maintenance,
                    "admission": app.state.v2.chat.admission.stats(),
                    "usage_budget": app.state.v2.chat.usage_budget.stats()}}

    async def perf_reset() -> dict:
        lag.ensure_started()
        if args.threadpool_workers > 0 and not marks.get("executor"):
            from concurrent.futures import ThreadPoolExecutor

            marks["executor"] = True
            asyncio.get_running_loop().set_default_executor(
                ThreadPoolExecutor(max_workers=args.threadpool_workers, thread_name_prefix="perf-worker"))
        lag.samples.clear()
        pool_peaks.reset()
        marks["cpu"], marks["wall"] = time.process_time(), time.time()
        if profiler is not None:
            profiler.clear()
        return {"ok": True}

    async def perf_profile(top: int = 45, sort: str = "tottime") -> dict:
        if profiler is None:
            return {"enabled": False}
        profiler.disable()
        try:
            profiler.dump_stats(args.profile)
            buffer = io.StringIO()
            pstats.Stats(profiler, stream=buffer).sort_stats(sort).print_stats(top)
        finally:
            profiler.enable()
        return {"enabled": True, "file": args.profile, "text": buffer.getvalue()}

    # 静态前端挂在 "/"，新路由必须排在它前面才匹配得到
    for path, endpoint, method in (("/__perf/stats", perf_stats, "GET"), ("/__perf/reset", perf_reset, "POST"),
                                   ("/__perf/profile", perf_profile, "GET")):
        app.router.routes.insert(0, APIRoute(path, endpoint, methods=[method]))

    if profiler is not None:
        profiler.enable()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
