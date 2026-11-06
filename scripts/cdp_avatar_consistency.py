"""验证头像一致性：信息流里 AI 人物头像 vs 人物主页头像（src 一致）。"""

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import time

import requests
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9342
OUT = "docs/screenshots/v3"


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def send(self, method, params=None):
        self.n += 1
        msg_id = self.n
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            data = json.loads(await self.ws.recv())
            if data.get("id") == msg_id:
                return data.get("result", {})

    async def eval(self, expression):
        res = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        return res.get("result", {}).get("value", res.get("result", {}).get("description"))

    async def shot(self, path):
        res = await self.send("Page.captureScreenshot", {"format": "png"})
        png = base64.b64decode(res["data"])
        os.makedirs(OUT, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(png)
        print("saved", path, len(png))


async def main():
    profile = tempfile.mkdtemp(prefix="chrome-avatar-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         "--window-size=500,900", "http://127.0.0.1:8000/#/discover"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = ""
        for _ in range(40):
            time.sleep(0.5)
            try:
                targets = requests.get(f"http://127.0.0.1:{PORT}/json/list", timeout=2).json()
                page = next((t for t in targets if t.get("type") == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                continue
        if not ws_url:
            raise SystemExit("no CDP target")

        async with websockets.connect(ws_url, max_size=40_000_000) as ws:
            cdp = CDP(ws)
            await cdp.send("Runtime.enable")
            time.sleep(5.0)

            feed_src = await cdp.eval(
                """
                (() => {
                  const btn = document.querySelector('.v3-avatar-btn img');
                  return btn ? btn.src.slice(0, 120) : 'none';
                })()
                """
            )
            print("feed avatar src:", feed_src)
            await cdp.shot(os.path.join(OUT, "v3-feed-avatar-consistency.png"))

            # 点第一个 AI 人物头像进主页
            print("open profile:", await cdp.eval(
                "document.querySelector('.v3-avatar-btn')?.click(), 'ok'"
            ))
            time.sleep(2.5)
            profile_src = await cdp.eval(
                """
                (() => {
                  const el = document.querySelector('[style*="background-image"]');
                  const img = document.querySelector('.w4-info-person img, img');
                  return JSON.stringify({
                    bg: el ? getComputedStyle(el).backgroundImage.slice(0, 120) : '',
                    img: img ? img.src.slice(0, 120) : ''
                  });
                })()
                """
            )
            print("profile avatar:", profile_src)
            await cdp.shot(os.path.join(OUT, "v3-persona-profile-avatar.png"))
    finally:
        proc.terminate()


asyncio.run(main())
