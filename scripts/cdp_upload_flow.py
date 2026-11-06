"""端到端：上传自己的照片 → 发布动态 → 信息流可见。"""

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import time

import requests
import websockets
from PIL import Image

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9334
OUT = "docs/screenshots/v3"
URL = "http://127.0.0.1:8000/#/discover"


def make_photo(path: str) -> None:
    img = Image.new("RGB", (900, 640), (240, 200, 140))
    px = img.load()
    for y in range(640):
        for x in range(0, 900, 3):
            px[x, y] = (int(90 + x / 12), int(60 + y / 10), 150)
    img.save(path, "PNG")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        msg_id = self.n
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            data = json.loads(await self.ws.recv())
            if data.get("id") == msg_id:
                return data.get("result", {})

    async def eval(self, expression: str) -> dict:
        res = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        return res.get("result", {})

    async def shot(self, path: str) -> None:
        res = await self.send("Page.captureScreenshot", {"format": "png"})
        png = base64.b64decode(res["data"])
        os.makedirs(OUT, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(png)
        print("saved", path, len(png))


async def main():
    photo = os.path.join(tempfile.mkdtemp(prefix="upl-"), "my-photo.png")
    make_photo(photo)
    profile = tempfile.mkdtemp(prefix="chrome-upl-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         "--window-size=460,900", URL],
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
            await cdp.send("DOM.enable")
            time.sleep(4)

            print("open:", await cdp.eval(
                "[...document.querySelectorAll('button')].find(b => (b.textContent||'').includes('发动态'))?.click(), 'ok'"
            ))
            time.sleep(1.0)

            doc = await cdp.send("DOM.getDocument", {"depth": -1})
            node = await cdp.send(
                "DOM.querySelector",
                {"nodeId": doc["root"]["nodeId"], "selector": "input[type=file]"},
            )
            node_id = node.get("nodeId")
            print("file input node:", node_id)
            if node_id:
                await cdp.send("DOM.setFileInputFiles", {"nodeId": node_id, "files": [photo]})
            time.sleep(2.5)

            print("uploaded:", await cdp.eval(
                "document.body.innerText.includes('上传中') ? 'uploading' : 'done'"
            ))
            await cdp.shot(os.path.join(OUT, "v3-upload-dialog.png"))

            # 填文案并发布
            await cdp.eval(
                """
                (() => {
                  const ta = document.querySelector('textarea');
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                  setter.call(ta, '我上传的第一张照片，测试一下。');
                  ta.dispatchEvent(new Event('input', { bubbles: true }));
                  return 'typed';
                })()
                """
            )
            time.sleep(0.5)
            print("publish:", await cdp.eval(
                "[...document.querySelectorAll('button')].filter(b => b.textContent.trim()==='发布').pop()?.click(), 'ok'"
            ))
            time.sleep(3.0)
            await cdp.shot(os.path.join(OUT, "v3-upload-feed.png"))
            print("in feed:", await cdp.eval(
                "document.body.innerText.includes('我上传的第一张照片') ? 'yes' : 'no'"
            ))
    finally:
        proc.terminate()


asyncio.run(main())
