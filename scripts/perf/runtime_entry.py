"""Run one benchmark tool with optional pure-dependency fallback and network guard."""
from __future__ import annotations
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
fallback = ROOT / ".venv" / "Lib" / "site-packages"
if fallback.is_dir():
    sys.path.append(str(fallback))
sys.path.insert(0, str(Path(__file__).resolve().parent))


if os.getenv("PERF_FAKE_ONLY") == "1":
    from perf_utils import install_loopback_guard
    install_loopback_guard()

tool = Path(sys.argv[1]).resolve()
if tool.parent != Path(__file__).resolve().parent or tool.suffix != ".py":
    raise SystemExit("Only scripts/perf Python tools may be launched")
sys.argv = [str(tool), *sys.argv[2:]]
runpy.run_path(str(tool), run_name="__main__")
