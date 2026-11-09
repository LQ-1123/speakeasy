"""实测：新用户加一个 Persona 好友 → 发一条消息 → 看来电语音条走 Fish 还是 Edge。"""

from __future__ import annotations

import os
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")
AUDIO = os.path.join(ROOT, "data", "audio")


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=120.0) as anon:
        reg = anon.post(
            "/api/v3/auth/register", json={"device_key": f"voice-check-{int(time.time())}"}
        )
        if reg.status_code != 200:
            print("注册失败:", reg.status_code, reg.text[:200])
            return 1
        data = reg.json()
        headers = {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}
        personas = anon.get("/api/v3/personas", params={"limit": 20}).json().get("items") or []
        target = next((p for p in personas if p.get("nickname") == "Adrian"), personas[0])
        print(f"人物：{target['nickname']}（{target['language']}）")
        check = anon.get(
            "/api/v3/stats/voice", params={"persona_id": target["id"]}
        ).json()
        item = (check.get("items") or [{}])[0]
        print("服务端绑定:", item.get("speech"))

    with httpx.Client(base_url=BASE, timeout=180.0, headers=headers) as client:
        add = client.post(f"/api/v3/personas/{target['id']}/friend", json={})
        print("加好友:", add.status_code)
        friend_id = add.json().get("friend_id") if add.status_code == 200 else ""
        if not friend_id:
            print(add.text[:200])
            return 1
        before = set(os.listdir(AUDIO))
        r = client.post(
            f"/api/v2/chat/{friend_id}/text",
            json={"text": "Hey, how's your day going?"},
        )
        print("聊天接口:", r.status_code, "" if r.status_code == 200 else r.text[:160])

    time.sleep(1.5)
    new_files = [n for n in os.listdir(AUDIO) if n not in before]
    print("新音频文件:", new_files or "（无）")
    providers = []
    for name in new_files:
        provider = (
            "fish"
            if name.startswith("fish_")
            else ("edge" if name.startswith("tts_") else "?")
        )
        providers.append(provider)
        print(f"  {name} → {provider}")
    print("结论:", "聊天语音走 Fish" if "fish" in providers else "聊天语音走 Edge（降级）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
