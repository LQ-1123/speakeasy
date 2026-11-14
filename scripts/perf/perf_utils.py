"""Local benchmark provenance, isolation and resource measurements."""
from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def install_loopback_guard() -> None:
    """Fail before network syscalls that could leave the host; fake cost is zero."""
    def guard(event, args):
        if event == "socket.getaddrinfo":
            host = args[0]
            if host is None or host in {"localhost", "127.0.0.1", "::1"}:
                return
            try:
                if ipaddress.ip_address(host).is_loopback:
                    return
            except ValueError:
                pass
            raise PermissionError("Fake-only budget is zero: non-loopback DNS blocked")
        if event in {"socket.connect", "socket.sendto"}:
            address = args[-1]
            if isinstance(address, tuple) and not ipaddress.ip_address(address[0]).is_loopback:
                raise PermissionError("Fake-only budget is zero: non-loopback connection blocked")
    sys.addaudithook(guard)


def require_loopback(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise ValueError("An explicit loopback provider URL is required")
    try:
        allowed = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise ValueError("Fake-only benchmarks permit loopback provider URLs only")
    return url.rstrip("/")


def isolated_env(data_dir: str, fake: str, inherited: dict | None = None) -> dict:
    """Preserve runtime paths, remove production settings and set fake-only ones."""
    fake = require_loopback(fake)
    source = dict(os.environ if inherited is None else inherited)
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PYTHONPATH",
            "VIRTUAL_ENV", "LANG", "LC_ALL"}
    env = {key: value for key, value in source.items() if key.upper() in keep}
    env.update({
        "SE_LOAD_DOTENV": "0", "DATA_DIR": str(Path(data_dir).resolve()),
        "LLM_BASE_URL": fake + "/v1", "LLM_API_KEY": "perf-fake-key", "LLM_MODEL": "fake-chat",
        "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "API_KEY": "",
        "STT_PROVIDER": "siliconflow", "STT_SILICONFLOW_API_KEY": "perf-fake-key",
        "STT_SILICONFLOW_BASE_URL": fake + "/v1", "DEEPGRAM_API_KEY": "perf-fake-key",
        "SE_EMAIL_CODE_AUTH": "1", "SE_REQUIRE_AUTH": "1", "SE_MEDIA_ENABLED": "0",
        "SE_MEDIA_SECRET": "perf-only-media-secret-0123456789abcdef",
        "SE_VOICE_STRICT": "1", "SE_TTS_FISH": "1", "SE_TTS_DEFER_AUDIO": "1",
        "SE_CHAT_AUTO_SUGGESTIONS": "0", "SE_BG_MEMORY": "0",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TTS_PREWARM": "0",
        "STT": '{"backend":"mock","provider":"siliconflow"}',
        "TTS": '{"backend":"tone","prewarm":false}', "VAD": '{"backend":"energy"}',
        "NO_PROXY": "127.0.0.1,localhost,::1", "PERF_FAKE_ONLY": "1",
        "PERF_EXTERNAL_PROVIDER_BUDGET": "0",
    })
    return env


def source_provenance(root: Path = ROOT) -> dict:
    """Hash tracked source and untracked source additions; never report file data."""
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
        hasher = hashlib.sha256()
        count = 0
        for raw in sorted(set(names.split(b"\0"))):
            if not raw:
                continue
            relative = raw.decode("utf-8")
            path = root / relative
            if (not path.is_file() or relative.startswith((".tmp/", "data/", ".venv/", "node_modules/"))
                    or path.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".json", ".toml", ".lock", ".txt", ".yml", ".yaml", ".md", ".html", ".css"}
                    or path.name.startswith(".env")):
                continue
            hasher.update(raw + b"\0" + path.read_bytes() + b"\0")
            count += 1
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root))
        return {"git_hash": revision, "dirty": dirty, "source_sha256": hasher.hexdigest(), "source_files": count}
    except (OSError, subprocess.CalledProcessError):
        return {"git_hash": None, "dirty": None, "source_sha256": None}


def resource_snapshot(pids: list[int]) -> dict:
    """Process tree includes children; host/cgroup scopes are labeled separately."""
    result = {"process_tree": {"available": False}, "host": {"available": False}, "cgroup": {"available": False}}
    try:
        import psutil
        processes = {}
        for pid in pids:
            try:
                process = psutil.Process(pid)
                processes[pid] = process
                for child in process.children(recursive=True):
                    processes[child.pid] = child
            except psutil.Error:
                continue
        cpu, rss = 0.0, 0
        for process in processes.values():
            try:
                times = process.cpu_times()
                cpu += times.user + times.system
                rss += process.memory_info().rss
            except psutil.Error:
                pass
        host_cpu = psutil.cpu_times()
        result["process_tree"] = {"available": True, "cpu_s": cpu,
            "rss_bytes": rss, "pids": sorted(processes), "scope": "owned pids plus live descendants"}
        result["host"] = {"available": True, "busy_cpu_s": sum(host_cpu) - host_cpu.idle - getattr(host_cpu, "iowait", 0),
            "memory_used_bytes": psutil.virtual_memory().used, "logical_cpus": psutil.cpu_count(),
            "scope": "whole host including unrelated processes"}
    except ImportError:
        pass
    try:
        location = Path("/sys/fs/cgroup")
        membership = Path("/proc/self/cgroup").read_text().splitlines()
        v2 = next((line.split(":", 2)[2] for line in membership if line.startswith("0::")), "/")
        target = location / v2.lstrip("/")
        if not (target / "cpu.stat").exists():
            target = location
        cpu = dict(line.split() for line in (target / "cpu.stat").read_text().splitlines())
        result["cgroup"] = {"available": True, "path": str(target),
            "cpu_s": int(cpu["usage_usec"]) / 1e6,
            "memory_bytes": int((target / "memory.current").read_text()),
            "cpu_max": (target / "cpu.max").read_text().strip(),
            "memory_max": (target / "memory.max").read_text().strip(),
            "scope": "current cgroup; may contain unrelated processes"}
    except (OSError, ValueError, KeyError, StopIteration):
        pass
    return result
