"""一键环境安装（Windows 优先，Linux/macOS 亦可）。

用法::

    python scripts/setup_env.py                  # 完整流程
    python scripts/setup_env.py --skip-torch     # torch 已装好
    python scripts/setup_env.py --cpu            # 无 GPU，装 CPU 版 torch
    python scripts/setup_env.py --check          # 只体检，不安装

设计原则：**每一步都可单独重试**，失败时打印可直接复制的修复命令，而不是一次性 abort。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
VENV = ROOT / ".venv"

PYPI_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"
TORCH_CU128 = "https://download.pytorch.org/whl/cu128"
TORCH_CPU = "https://download.pytorch.org/whl/cpu"

IS_WIN = platform.system() == "Windows"
PY = str(VENV / "Scripts" / "python.exe") if IS_WIN else str(VENV / "bin" / "python")
PIP = [PY, "-m", "pip"]


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
class C:
    OK = "\033[92m"
    WARN = "\033[93m"
    ERR = "\033[91m"
    DIM = "\033[2m"
    B = "\033[1m"
    X = "\033[0m"
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        OK = WARN = ERR = DIM = B = X = ""


def say(msg: str) -> None:
    print(msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    say(f"\n{C.B}[{n}/{total}]{C.X} {C.B}{msg}{C.X}")


def ok(msg: str) -> None:
    say(f"  {C.OK}✓{C.X} {msg}")


def warn(msg: str) -> None:
    say(f"  {C.WARN}!{C.X} {msg}")


def err(msg: str) -> None:
    say(f"  {C.ERR}✗{C.X} {msg}")


def info(msg: str) -> None:
    say(f"  {C.DIM}{msg}{C.X}")


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, quiet: bool = False) -> int:
    info(" ".join(cmd))
    try:
        if quiet:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            if p.returncode != 0 and check:
                say(p.stdout[-2000:])
                say(p.stderr[-2000:])
            return p.returncode
        return subprocess.run(cmd, cwd=cwd, check=check).returncode
    except FileNotFoundError:
        err(f"找不到命令: {cmd[0]}")
        return 127
    except subprocess.CalledProcessError as e:
        return e.returncode


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #
def find_python311() -> str | None:
    """找一个可用的 3.10-3.12 解释器。"""
    candidates: list[list[str]] = []
    if IS_WIN:
        candidates.append(["py", "-3.11"])
        candidates.append(["py", "-3.12"])
        candidates.append(["py", "-3.10"])
        for minor in (11, 12, 10):
            for base in (
                rf"C:\Users\{os.getenv('USERNAME')}\AppData\Local\Programs\Python\Python3{minor}\python.exe",
                rf"C:\Python3{minor}\python.exe",
            ):
                if Path(base).exists():
                    candidates.append([base])
    candidates.append(["python3.11"])
    candidates.append(["python3"])

    for cmd in candidates:
        try:
            p = subprocess.run(
                [*cmd, "-c", "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                capture_output=True, text=True, timeout=15,
            )
            if p.returncode == 0:
                ver = p.stdout.strip()
                if ver in ("3.10", "3.11", "3.12"):
                    return " ".join(cmd)
                info(f"跳过 {cmd[0]} (版本 {ver}，需要 3.10-3.12)")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def check_torch() -> dict:
    """在 venv 里检查 torch —— 真的跑一次 kernel。"""
    code = r"""
import json, sys
out = {"installed": False}
try:
    import torch
    out["installed"] = True
    out["version"] = torch.__version__
    out["cuda_build"] = torch.version.cuda
    out["cuda_available"] = bool(torch.cuda.is_available())
    if out["cuda_available"]:
        p = torch.cuda.get_device_properties(0)
        out["device"] = p.name
        out["capability"] = f"{p.major}.{p.minor}"
        out["vram_gb"] = round(p.total_memory / 1024**3, 1)
        # 真跑 kernel，验证 sm_XX 有实现
        try:
            a = torch.randn(512, 512, device="cuda", dtype=torch.float16)
            b = a @ a
            torch.cuda.synchronize()
            out["kernel_ok"] = True
            out["_mean"] = round(float(b.abs().mean()), 4)
        except Exception as e:
            out["kernel_ok"] = False
            out["kernel_error"] = f"{type(e).__name__}: {e}"
except Exception as e:
    out["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
"""
    try:
        p = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=180)
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(p.stdout.strip().splitlines()[-1])
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {"installed": False}


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def check_nvidia() -> dict:
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        )
        if p.returncode == 0 and p.stdout.strip():
            parts = [x.strip() for x in p.stdout.strip().splitlines()[0].split(",")]
            return {"ok": True, "name": parts[0], "vram": parts[1], "driver": parts[2]}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return {"ok": False}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def do_check() -> int:
    say(f"\n{C.B}=== SpeakEasy 环境体检 ==={C.X}\n")

    nv = check_nvidia()
    if nv["ok"]:
        ok(f"GPU: {nv['name']} | {nv['vram']} | 驱动 {nv['driver']}")
    else:
        warn("nvidia-smi 不可用（无 NVIDIA 显卡或驱动未装）—— 将只能跑 CPU/mock 后端")

    if IS_WIN:
        if sys.version_info[:2] not in ((3, 10), (3, 11), (3, 12)):
            err(f"当前 Python {sys.version_info[0]}.{sys.version_info[1]} 不受支持（需 3.10-3.12）")
        else:
            ok(f"当前 Python {sys.version_info[0]}.{sys.version_info[1]}")
    else:
        ok(f"Python {platform.python_version()}")

    if VENV.exists():
        ok(f"虚拟环境存在: {VENV}")
    else:
        warn("虚拟环境不存在（将创建 .venv）")

    t = check_torch()
    if not t.get("installed"):
        err("torch 未安装")
    elif not t.get("cuda_available"):
        warn(f"torch {t.get('version')} 已装，但 CUDA 不可用（cu build: {t.get('cuda_build')}）")
    elif not t.get("kernel_ok"):
        err(f"CUDA 可用但 kernel 失败: {t.get('kernel_error')}")
        err("→ torch 的 CUDA 版本不含本卡 sm_XX，必须重装 cu128+ 版")
    else:
        ok(f"torch {t['version']} | CUDA {t.get('cuda_build')} | {t.get('device')} "
           f"sm_{t.get('capability', '?').replace('.', '')} | {t.get('vram_gb')}GB")
        ok("fp16 matmul kernel 实测通过")

    if check_ffmpeg():
        ok("ffmpeg 可用")
    else:
        warn("ffmpeg 未安装 → 非 wav 音频解码会失败")
        info("winget install --id Gyan.FFmpeg -e")

    hf = os.getenv("HF_ENDPOINT")
    if hf:
        ok(f"HF_ENDPOINT={hf}")
    else:
        warn("未设置 HF_ENDPOINT，国内可能下载超时")
        info("设 .env 里 HF_ENDPOINT=https://hf-mirror.com")

    for f, name in (
        (ROOT / ".env", ".env 配置文件"),
        (ROOT / "assets" / "ref_audio", "参考音频目录"),
        (ROOT / "backend" / "requirements.txt", "requirements.txt"),
    ):
        if f.exists():
            ok(name)
        else:
            warn(f"{name} 缺失: {f}")

    # 引擎探测
    say(f"\n{C.B}=== 引擎探测 ==={C.X}\n")
    probe = r"""
import json, os, sys
sys.path.insert(0, r"%s")
res = {}
for mod, key in (("faster_whisper", "faster_whisper"), ("silero_vad", "silero_vad"),
                 ("cosyvoice.cli.cosyvoice", "cosyvoice"), ("f5_tts.api", "f5_tts")):
    try:
        __import__(mod); res[key] = "ok"
    except Exception as e:
        res[key] = f"missing"
try:
    import numpy; res["numpy"] = numpy.__version__
except Exception: res["numpy"] = "missing"
try:
    import fastapi; res["fastapi"] = fastapi.__version__
except Exception: res["fastapi"] = "missing"
try:
    import openai; res["openai"] = openai.__version__
except Exception: res["openai"] = "missing"
print(json.dumps(res))
""" % str(BACKEND)
    try:
        p = subprocess.run([PY, "-c", probe], capture_output=True, text=True, timeout=120)
        r = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {}
    except Exception:
        r = {}

    if r:
        for k, label in (
            ("numpy", "numpy"), ("fastapi", "FastAPI"), ("openai", "openai SDK"),
            ("faster_whisper", "faster-whisper (STT)"), ("silero_vad", "silero-vad (VAD)"),
            ("cosyvoice", "CosyVoice (TTS)"), ("f5_tts", "F5-TTS (TTS)"),
        ):
            v = r.get(k, "missing")
            if v == "missing":
                warn(f"{label}: 未安装")
            else:
                ok(f"{label}: {v}")

        if r.get("cosyvoice") == "missing" and r.get("f5_tts") == "missing":
            warn("两个 TTS 克隆引擎都不可用 → 将使用内置 tone 后端（能跑通链路，但不好听）")

    say("")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="SpeakEasy 环境安装")
    ap.add_argument("--check", action="store_true", help="只体检不安装")
    ap.add_argument("--skip-torch", action="store_true", help="跳过 torch 安装")
    ap.add_argument("--skip-req", action="store_true", help="跳过 requirements 安装")
    ap.add_argument("--cpu", action="store_true", help="装 CPU 版 torch")
    ap.add_argument("--no-mirror", action="store_true", help="直连 PyPI，不用阿里云镜像")
    args = ap.parse_args()

    if args.check:
        return do_check()

    total = 5
    say(f"{C.B}SpeakEasy 环境安装{C.X}  ({platform.system()} {platform.release()})")

    # ---- 1. 找解释器 ----
    step(1, total, "检查 Python 解释器")
    base = find_python311()
    if not base:
        err("找不到 Python 3.10/3.11/3.12")
        info("下载: https://www.python.org/downloads/release/python-3119/")
        info("安装时务必勾选 'Add python.exe to PATH'")
        return 1
    ok(f"使用: {base}")

    # ---- 2. 建 venv ----
    step(2, total, "创建虚拟环境 .venv")
    if VENV.exists():
        ok("已存在，复用")
    else:
        cmd = base.split() + ["-m", "venv", str(VENV)]
        if run(cmd) != 0:
            err("创建虚拟环境失败")
            return 1
        ok(f"已创建: {VENV}")
    run(PIP + ["install", "--upgrade", "pip", "setuptools", "wheel", "--quiet"], check=False)

    # ---- 3. torch ----
    step(3, total, "安装 PyTorch (CUDA)")
    if args.skip_torch:
        ok("已跳过")
    else:
        existing = check_torch()
        if existing.get("installed") and existing.get("cuda_available") and existing.get("kernel_ok"):
            ok(f"已有可用 torch {existing['version']}，跳过")
        else:
            if existing.get("installed"):
                warn("现有 torch 不可用/不匹配，先卸载")
                run(PIP + ["uninstall", "-y", "torch", "torchaudio", "torchvision"], check=False, quiet=True)

            idx = TORCH_CPU if args.cpu else TORCH_CU128
            ok(f"索引: {idx}")
            info("下载约 2.5GB，请耐心等待…")
            if run(PIP + ["install", "torch", "torchaudio", "--index-url", idx]) != 0:
                err("torch 安装失败")
                info(f"手动: {PY} -m pip install torch torchaudio --index-url {idx}")
                return 1

            t = check_torch()
            if not t.get("cuda_available"):
                warn("torch 装好了但 CUDA 不可用")
                if not args.cpu:
                    err("→ 若你的卡是 RTX 50 系（sm_120），必须用 cu128 索引重装")
            elif not t.get("kernel_ok"):
                err(f"kernel 实测失败: {t.get('kernel_error')}")
                return 1
            else:
                ok(f"{t.get('device')} sm_{t.get('capability', '').replace('.', '')} OK")

    # ---- 4. requirements ----
    step(4, total, "安装后端依赖")
    if args.skip_req:
        ok("已跳过")
    else:
        mirror = [] if args.no_mirror else ["-i", PYPI_MIRROR]
        if run(PIP + ["install", "-r", str(BACKEND / "requirements.txt"), *mirror]) != 0:
            warn("部分依赖失败，继续尝试（可能是可选包）")
        else:
            ok("安装完成")

    # ---- 5. 配置文件 ----
    step(5, total, "初始化配置")
    env_example = ROOT / ".env.example"
    env = ROOT / ".env"
    if env.exists():
        ok(".env 已存在，未覆盖")
    elif env_example.exists():
        shutil.copy(env_example, env)
        ok("已从 .env.example 创建 .env")
    else:
        warn(".env.example 缺失")

    for d in ("assets/ref_audio", "data", "pretrained_models", "third_party"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    ok("目录已就绪")

    # ---- 总结 ----
    say(f"\n{C.OK}{'=' * 56}{C.X}")
    say(f"{C.OK}{C.B}  安装完成{C.X}")
    say(f"{C.OK}{'=' * 56}{C.X}\n")
    say(f"{C.B}下一步：{C.X}\n")
    activate = r".\.venv\Scripts\Activate.ps1" if IS_WIN else "source .venv/bin/activate"
    say(f"  1. 激活环境:      {activate}")
    say(f"  2. 填 LLM Key:    编辑 .env 里的 LLM_API_KEY")
    say(f"  3. 下载模型:      python scripts/download_models.py")
    say(f"  4. 准备参考音频:  python scripts/record_reference.py --persona oliver")
    say(f"  5. 体检:          python scripts/setup_env.py --check")
    say(f"  6. 启动:          python -m uvicorn app.main:app --app-dir backend --port 8000")
    say(f"\n  浏览器打开 http://127.0.0.1:8000\n")

    return do_check()


if __name__ == "__main__":
    sys.exit(main())
