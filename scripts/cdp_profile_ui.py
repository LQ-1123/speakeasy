"""截图：四图布局 / 个人资料兴趣 / 编辑资料兴趣编辑器。"""

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
PORT = 9335
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
        return res.get("result", {}).get("value")

    async def shot(self, path):
        res = await self.send("Page.captureScreenshot", {"format": "png"})
        png = base64.b64decode(res["data"])
        os.makedirs(OUT, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(png)
        print("saved", path, len(png))


async def main():
    profile = tempfile.mkdtemp(prefix="chrome-prof-")
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
            await cdp.send("Page.enable")
            time.sleep(4.5)
            await cdp.shot(os.path.join(OUT, "v3-feed-4images.png"))

            # 切到「我」
            await cdp.eval("location.hash = '#/me'")
            time.sleep(1.5)
            await cdp.shot(os.path.join(OUT, "v3-me-interests.png"))

            # 打开编辑资料
            await cdp.eval(
                "[...document.querySelectorAll('button')].find(b => (b.getAttribute('aria-label')||'') === '编辑资料')?.click(), 'ok'"
            )
            time.sleep(1.2)
            await cdp.shot(os.path.join(OUT, "v3-me-edit-top.png"))
            await cdp.eval(
                """
                (() => {
                  const label = [...document.querySelectorAll('div')]
                    .find(d => (d.textContent || '').trim().startsWith('兴趣标签（影响偶遇与人物匹配）'));
                  if (label) label.scrollIntoView({ block: 'center' });
                  return 'scrolled';
                })()
                """
            )
            time.sleep(0.6)
            await cdp.shot(os.path.join(OUT, "v3-me-interests-edit.png"))
    finally:
        proc.terminate()


asyncio.run(main())
