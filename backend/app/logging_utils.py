"""日志工具：统一格式 + 颜色（Windows 终端友好）+ 可选的延迟打点。"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator

_CONFIGURED = False

_LEVEL_COLORS = {
    "DEBUG": "\x1b[36m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[41;97m",
}
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"


def _supports_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


class _Formatter(logging.Formatter):
    def __init__(self, color: bool) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-24s %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self.color:
            c = _LEVEL_COLORS.get(record.levelname, "")
            msg = f"{_DIM}{msg[:9]}{_RESET}{c}{msg[9:]}{_RESET}"
        return msg


def configure_logging(level: str = "INFO", *, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(_supports_color()))
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # 第三方库降噪
    for noisy in (
        "httpcore",
        "httpx",
        "urllib3",
        "faster_whisper",
        "huggingface_hub",
        "filelock",
        "asyncio",
        "websockets",
        "matplotlib",
        "numba",
        "torch",
        "torchaudio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class Stopwatch:
    """轻量打点器：`sw.mark("stt")` → `sw.elapsed_ms("stt")`。"""

    __slots__ = ("_t0", "_marks")

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._marks: dict[str, float] = {}

    def mark(self, name: str) -> float:
        dt = (time.perf_counter() - self._t0) * 1000.0
        self._marks[name] = dt
        return dt

    def elapsed_ms(self, name: str | None = None) -> float:
        if name is None:
            return (time.perf_counter() - self._t0) * 1000.0
        return self._marks.get(name, (time.perf_counter() - self._t0) * 1000.0)

    def since(self, name: str) -> float:
        base = self._marks.get(name)
        if base is None:
            return self.elapsed_ms()
        return self.elapsed_ms() - base

    def as_dict(self) -> dict[str, float]:
        out = {k: round(v, 2) for k, v in self._marks.items()}
        out["total_ms"] = round(self.elapsed_ms(), 2)
        return out


@contextmanager
def timed(label: str, logger: logging.Logger | None = None) -> Iterator[dict[str, float]]:
    """`with timed("tts", log) as t: ...` → t["ms"] 记录耗时。"""
    t: dict[str, float] = {}
    start = time.perf_counter()
    try:
        yield t
    finally:
        t["ms"] = (time.perf_counter() - start) * 1000.0
        if logger is not None:
            logger.debug("%s took %.1f ms", label, t["ms"])
