"""截图：母语设置 + 分语言水平；以及按在练语言过滤后的信息流。"""

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
PORT = 9341
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
    profile = tempfile.mkdtemp(prefix="chrome-lang-")
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

        async with websockets.connect(ws_url, max_size=30_000_000) as ws:
            cdp = CDP(ws)
            await cdp.send("Runtime.enable")
            time.sleep(4.5)
            await cdp.shot(os.path.join(OUT, "v3-feed-lang-filter.png"))

            await cdp.eval("location.hash = '#/me'")
            time.sleep(1.5)
            await cdp.shot(os.path.join(OUT, "v3-me-langs.png"))

            await cdp.eval(
                "[...document.querySelectorAll('button')].find(b => (b.getAttribute('aria-label')||'')==='编辑资料')?.click(), 'ok'"
            )
            time.sleep(1.2)
            await cdp.eval(
                """
                (() => {
                  const label = [...document.querySelectorAll('div')]
                    .find(d => (d.textContent || '').trim().startsWith('母语（用于解释/翻译）'));
                  if (label) label.scrollIntoView({ block: 'start' });
                  return 'scrolled';
                })()
                """
            )
            time.sleep(0.6)
            await cdp.shot(os.path.join(OUT, "v3-me-edit-langs.png"))
    finally:
        proc.terminate()


asyncio.run(main())
