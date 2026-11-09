"""体检：Fish API 连通性 + 音色库可用性。"""

import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

cfg = json.load(open(os.path.join(ROOT, "data", "tts_config.json"), encoding="utf-8"))
key = cfg.get("fish_api_key", "")
base = cfg.get("fish_base_url", "https://api.fish.audio")
model = cfg.get("fish_model", "s2.1-pro")
print(f"base={base} model={model} key={key[:10]}…")

from app.voice import fish_tts  # noqa: E402


async def main() -> None:
    t0 = time.time()
    try:
        rows = await fish_tts.list_models(api_key=key, base_url=base, page_size=5)
        print(f"list_models OK: {len(rows)} 条, {time.time() - t0:.1f}s")
        for row in rows[:3]:
            print("  -", row["id"], row["languages"][:3], row["tags"][:3], row["title"][:20])
    except Exception as exc:  # noqa: BLE001
        print(f"list_models FAIL: {type(exc).__name__}: {exc}")

    t0 = time.time()
    try:
        audio = await fish_tts.synthesize(
            "Hello, quick voice check.",
            api_key=key,
            base_url=base,
            model=model,
            reference_id=cfg.get("fish_reference_id", ""),
            audio_format="mp3",
        )
        print(f"tts OK: {len(audio)} bytes, {time.time() - t0:.1f}s")
    except Exception as exc:  # noqa: BLE001
        print(f"tts FAIL: {type(exc).__name__}: {exc}")


asyncio.run(main())
