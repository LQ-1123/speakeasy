"""用 CDP 打开「发动态」弹窗并截图（验证内置图片 + 自定义标签 UI）。"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time

import requests
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9333
OUT_DIR = "docs/screenshots/v3"
URL = "http://127.0.0.1:8000/#/discover"


async def cdp(ws_url: str, expression: str) -> dict:
    async with websockets.connect(ws_url, max_size=20_000_000) as ws:
        counter = {"n": 0}

        async def send(method: str, params: dict) -> dict:
            counter["n"] += 1
            msg_id = counter["n"]
            await ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
            while True:
                raw = await ws.recv()
                data = json.loads(raw)
                if data.get("id") == msg_id:
                    return data

        await send("Runtime.enable", {})
        await send("Page.enable", {})
        result = await send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        return result


async def screenshot(ws_url: str, path: str) -> None:
    async with websockets.connect(ws_url, max_size=40_000_000) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
        while True:
            data = json.loads(await ws.recv())
            if data.get("id") == 1:
                png = base64.b64decode(data["result"]["data"])
                os.makedirs(OUT_DIR, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(png)
                print("saved", path, len(png), "bytes")
                return


async def main():
    profile = tempfile.mkdtemp(prefix="chrome-cdp-")
    proc = subprocess.Popen(
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
            f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
            "--window-size=460,900", URL,
        ],
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
        time.sleep(4)  # 等前端渲染
        await cdp(ws_url, "1")  # 热身

        opened = await cdp(
            ws_url,
            """
            (() => {
              const btn = [...document.querySelectorAll('button')]
                .find(b => (b.textContent || '').includes('发动态'));
              if (!btn) return 'no button';
              btn.click();
              return 'clicked';
            })()
            """,
        )
        print("open:", opened.get("result", {}).get("result"))
        time.sleep(1.2)
        shot = os.path.join(OUT_DIR, "v3-publish-dialog.png")
        await screenshot(ws_url, shot)

        # 添加自定义标签并截图
        added = await cdp(
            ws_url,
            """
            (() => {
              const input = [...document.querySelectorAll('input')]
                .find(i => (i.placeholder || '').includes('自定义标签'));
              if (!input) return 'no input';
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
              setter.call(input, 'AI 工具');
              input.dispatchEvent(new Event('input', { bubbles: true }));
              const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '添加');
              if (btn) btn.click();
              return 'added';
            })()
            """,
        )
        print("tag:", added.get("result", {}).get("result"))
        time.sleep(0.6)
        scrolled = await cdp(
            ws_url,
            """
            (() => {
              const label = [...document.querySelectorAll('div')]
                .find(d => (d.textContent || '').trim().startsWith('兴趣标签'));
              if (label) label.scrollIntoView({ block: 'center' });
              return document.body.innerText.includes('AI 工具') ? 'chip-present' : 'chip-missing';
            })()
            """,
        )
        print("scroll:", scrolled.get("result", {}).get("result"))
        time.sleep(0.6)
        await screenshot(ws_url, os.path.join(OUT_DIR, "v3-publish-tag.png"))
    finally:
        proc.terminate()


asyncio.run(main())
