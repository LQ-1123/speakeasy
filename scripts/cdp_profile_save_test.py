"""端到端验证：编辑资料 → 保存 → 刷新后仍是新资料。"""

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
PORT = 9336
OUT = "docs/screenshots/v3"
NEW_NAME = "测试昵称A"


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
    profile = tempfile.mkdtemp(prefix="chrome-save-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         "--window-size=500,900", "http://127.0.0.1:8000/#/me"],
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

            print("open edit:", await cdp.eval(
                "[...document.querySelectorAll('button')].find(b => (b.getAttribute('aria-label')||'')==='编辑资料')?.click(), 'ok'"
            ))
            time.sleep(1.2)
            print("type name:", await cdp.eval(
                """
                (() => {
                  const inputs = [...document.querySelectorAll('input')];
                  const nameInput = inputs.find(i => (i.value || '') !== '' && i.type !== 'file') || inputs[0];
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                  setter.call(nameInput, '测试昵称A');
                  nameInput.dispatchEvent(new Event('input', { bubbles: true }));
                  return nameInput.value;
                })()
                """
            ))
            print("pick male:", await cdp.eval(
                "[...document.querySelectorAll('button')].find(b => b.textContent.trim()==='男')?.click(), 'ok'"
            ))
            time.sleep(0.4)
            await cdp.shot(os.path.join(OUT, "v3-me-edit-before-save.png"))
            print("save:", await cdp.eval(
                "[...document.querySelectorAll('button')].find(b => ['保存','保存中…'].includes(b.textContent.trim()))?.click(), 'ok'"
            ))
            time.sleep(5.0)
            await cdp.shot(os.path.join(OUT, "v3-me-after-save.png"))
            print("reloaded shows new name:", await cdp.eval(
                "document.body.innerText.includes('测试昵称A') ? 'yes' : 'no'"
            ))
    finally:
        proc.terminate()


asyncio.run(main())
