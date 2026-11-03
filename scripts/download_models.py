"""模型权重下载（走 HF 镜像，可断点续传）。

用法::

    python scripts/download_models.py                    # 下载 STT + 默认 TTS
    python scripts/download_models.py --stt large-v3
    python scripts/download_models.py --stt none --tts f5tts
    python scripts/download_models.py --list             # 只列出可用项
    python scripts/download_models.py --tts cosyvoice2 --verify-only

设计：优先用 huggingface_hub.snapshot_download（自动续传、缓存复用），
失败则打印手动 wget/curl 命令。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STT_MODELS = {
    "distil-large-v3": ("Systran/faster-distil-whisper-large-v3", "~1.5 GB", "推荐：快 2 倍，英文接近 large-v3"),
    "large-v3": ("Systran/faster-whisper-large-v3", "~3.1 GB", "最准，速度 1x"),
    "medium": ("Systran/faster-whisper-medium", "~1.5 GB", "够用，速度快"),
    "small": ("Systran/faster-whisper-small", "~0.5 GB", "只做演示"),
    "tiny": ("Systran/faster-whisper-tiny", "~75 MB", "极限压缩"),
}

TTS_MODELS = {
    "f5tts": {
        "repo": "SWivid/F5-TTS",
        "files": ["F5TTS_Base/model_1200000.safetensors", "F5TTS_Base/vocab.txt"],
        "size": "~1.4 GB",
        "note": "Windows 首选",
    },
    "cosyvoice2": {
        "repo": "FunAudioLLM/CosyVoice2-0.5B",
        "files": [
            "cosyvoice2.yaml", "llm.pt", "flow.pt", "hift.pt",
            "campplus.onnx", "speech_tokenizer_v2.onnx",
        ],
        "size": "~2.0 GB",
        "note": "零样本克隆更强，Windows 依赖较难装",
    },
}


class C:
    OK = "\033[92m" if sys.stdout.isatty() else ""
    WARN = "\033[93m" if sys.stdout.isatty() else ""
    ERR = "\033[91m" if sys.stdout.isatty() else ""
    DIM = "\033[2m" if sys.stdout.isatty() else ""
    B = "\033[1m" if sys.stdout.isatty() else ""
    X = "\033[0m" if sys.stdout.isatty() else ""


def setup_env() -> None:
    """必须在 import huggingface_hub 之前设置镜像。"""
    env_file = ROOT / ".env"
    endpoint = os.getenv("HF_ENDPOINT", "")
    if not endpoint and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("HF_ENDPOINT=") and not line.startswith("#"):
                endpoint = line.split("=", 1)[1].strip()
                break
    if not endpoint:
        endpoint = "https://hf-mirror.com"
    os.environ.setdefault("HF_ENDPOINT", endpoint)
    print(f"{C.DIM}HF_ENDPOINT = {endpoint}{C.X}")


def list_models() -> None:
    print(f"\n{C.B}STT (faster-whisper){C.X}")
    for k, (repo, size, note) in STT_MODELS.items():
        print(f"  {k:<18} {size:>9}  {repo:<42} {C.DIM}{note}{C.X}")
    print(f"\n{C.B}TTS{C.X}")
    for k, v in TTS_MODELS.items():
        print(f"  {k:<18} {v['size']:>9}  {v['repo']:<42} {C.DIM}{v['note']}{C.X}")
    print()


def download_stt(name: str) -> bool:
    if name not in STT_MODELS:
        print(f"{C.ERR}未知 STT 模型: {name}{C.X}（可用: {', '.join(STT_MODELS)}）")
        return False
    repo, size, _ = STT_MODELS[name]
    print(f"\n{C.B}[STT]{C.X} {repo}  {C.DIM}({size}){C.X}")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(f"{C.ERR}huggingface_hub 未安装{C.X}  → pip install huggingface_hub")
        return False
    try:
        path = snapshot_download(
            repo_id=repo,
            allow_patterns=["*.bin", "*.json", "*.txt", "*.model", "*.safetensors"],
            resume_download=True,
        )
        print(f"{C.OK}✓{C.X} 已缓存到 {path}")
        return True
    except Exception as e:
        print(f"{C.ERR}✗ 下载失败: {type(e).__name__}: {e}{C.X}")
        print(f"{C.DIM}手动下载：{C.X}")
        print(f"  pip install huggingface_hub")
        print(f"  $env:HF_ENDPOINT='https://hf-mirror.com'")
        print(f"  huggingface-cli download {repo}")
        return False


def download_tts(name: str) -> bool:
    if name not in TTS_MODELS:
        print(f"{C.ERR}未知 TTS: {name}{C.X}")
        return False
    spec = TTS_MODELS[name]
    print(f"\n{C.B}[TTS]{C.X} {spec['repo']}  {C.DIM}({spec['size']}){C.X}")

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print(f"{C.ERR}huggingface_hub 未安装{C.X}  → pip install huggingface_hub")
        return False

    ok_count = 0
    for f in spec["files"]:
        print(f"  {C.DIM}→ {f}{C.X}")
        try:
            p = hf_hub_download(repo_id=spec["repo"], filename=f, resume_download=True)
            print(f"    {C.OK}✓{C.X} {p}")
            ok_count += 1
        except Exception as e:
            print(f"    {C.ERR}✗ {type(e).__name__}: {str(e)[:160]}{C.X}")

    if ok_count == len(spec["files"]):
        print(f"{C.OK}✓ {name} 完整下载{C.X}")
        if name == "cosyvoice2":
            target = ROOT / "pretrained_models" / "CosyVoice2-0.5B"
            print(f"  {C.DIM}提示：把缓存目录软链/复制到 {target}，或在 .env 里设 COSYVOICE_MODEL_DIR 指向缓存路径{C.X}")
        elif name == "f5tts":
            print(f"  {C.DIM}提示：F5-TTS 会自动从 HF 缓存找权重，通常无需额外配置{C.X}")
        return True

    print(f"{C.ERR}部分文件失败（{ok_count}/{len(spec['files'])}）{C.X}")
    print(f"{C.DIM}可重试同一命令（已下载部分会复用）{C.X}")
    return False


def verify() -> int:
    """检查已下载的模型是否可用。"""
    print(f"\n{C.B}=== 模型体检 ==={C.X}")
    problems = 0
    try:
        os.environ.setdefault("HF_HUB_OFFLINE", "0")
        import faster_whisper  # noqa: F401
        print(f"{C.OK}✓{C.X} faster-whisper 可 import")
    except ImportError:
        print(f"{C.ERR}✗{C.X} faster-whisper 未安装")
        problems += 1

    for name in ("silero_vad",):
        try:
            __import__(name)
            print(f"{C.OK}✓{C.X} {name} 可 import")
        except ImportError:
            print(f"{C.WARN}~{C.X} {name} 未安装（VAD 会降级为能量检测）")

    for name, mod in (("CosyVoice", "cosyvoice"), ("F5-TTS", "f5_tts")):
        try:
            __import__(mod)
            print(f"{C.OK}✓{C.X} {name} 可 import")
        except ImportError:
            print(f"{C.WARN}~{C.X} {name} 未安装（TTS 会降级为 tone）")

    print(f"\n{C.DIM}注：import 成功 ≠ 权重就绪，最终以下载命令的输出为准。{C.X}")
    return 0 if problems == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 SpeakEasy 模型权重")
    ap.add_argument("--stt", default="distil-large-v3", help="STT 模型名，或 'none' 跳过")
    ap.add_argument("--tts", default="f5tts", help="TTS 引擎，或 'none' 跳过")
    ap.add_argument("--list", action="store_true", help="列出可用模型")
    ap.add_argument("--verify-only", action="store_true", help="只体检已装库")
    args = ap.parse_args()

    if args.list:
        list_models()
        return 0

    if args.verify_only:
        return verify()

    setup_env()
    results = []

    if args.stt.lower() != "none":
        results.append(("STT", download_stt(args.stt)))
    if args.tts.lower() != "none":
        results.append(("TTS", download_tts(args.tts)))

    print(f"\n{C.B}=== 结果 ==={C.X}")
    failed = 0
    for name, okv in results:
        mark = f"{C.OK}✓{C.X}" if okv else f"{C.ERR}✗{C.X}"
        print(f"  {mark} {name}")
        if not okv:
            failed += 1

    if failed:
        print(f"\n{C.WARN}有 {failed} 项失败。可重试，或用 'tone'/'mock' 后端先跑通链路。{C.X}")
    else:
        print(f"\n{C.OK}全部就绪。下一步：准备参考音频 → python scripts/record_reference.py{C.X}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
