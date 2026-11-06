"""验证：点第二句朗读时第一句被停止（starts=2, stops>=1，同一时刻只有一个）。"""

import asyncio
import json
import subprocess
import tempfile
import time

import requests
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9339


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
        out = res.get("result", {})
        return out.get("value", out.get("description"))


async def main():
    profile = tempfile.mkdtemp(prefix="chrome-audio2-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--autoplay-policy=no-user-gesture-required",
         "--no-first-run", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
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

        async with websockets.connect(ws_url, max_size=20_000_000) as ws:
            cdp = CDP(ws)
            await cdp.send("Runtime.enable")
            time.sleep(5.0)

            print("click1:", await cdp.eval(
                """
                (() => {
                  const btns = [...document.querySelectorAll('button')].filter(b => (b.textContent||'').includes('朗读'));
                  if (!btns.length) return 'no-button';
                  btns[0].scrollIntoView({block:'center'});
                  btns[0].click();
                  return 'clicked:' + btns.length;
                })()
                """
            ))
            time.sleep(1.5)
            print("stats1:", await cdp.eval("JSON.stringify(window.__speakeasyAudio?.stats?.())"))

            print("click2:", await cdp.eval(
                """
                (() => {
                  const btns = [...document.querySelectorAll('button')].filter(b => (b.textContent||'').includes('朗读'));
                  if (btns.length < 2) return 'not-enough';
                  btns[1].click();
                  return 'clicked';
                })()
                """
            ))
            time.sleep(1.2)
            print("stats2:", await cdp.eval("JSON.stringify(window.__speakeasyAudio?.stats?.())"))
            print("only-one-playing:", await cdp.eval(
                "window.__speakeasyAudio?.webAudio?.() ? 'yes' : 'no'"
            ))
    finally:
        proc.terminate()


asyncio.run(main())
