"""Minimal CDP driver (Chrome DevTools Protocol) for local/remote Web verification.

Why not Playwright: the repo has no such dependency and we only need "open page +
real interactions + screenshots + console/network assertions". Chrome is installed,
so raw CDP is the most controllable option.

Note for this machine: a Python ``subprocess`` cannot launch Chrome (the DSH job
object makes Chrome exit with STATUS_BREAKPOINT), so the verification scripts
**attach** to a Chrome started by PowerShell
(``Start-Process chrome.exe --headless=new --remote-debugging-port=<port> ...``).
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import websockets.sync.client as ws_client

DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CDPError(RuntimeError):
    pass


class Chrome:
    """One Chrome instance (or an attached one)."""

    def __init__(
        self,
        *,
        chrome_path: str = DEFAULT_CHROME,
        user_data_dir: str | None = None,
        headless: bool = True,
        width: int = 1440,
        height: int = 900,
        mobile: bool = False,
    ) -> None:
        self.chrome_path = chrome_path
        self.tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        if user_data_dir:
            self.user_data_dir = user_data_dir
        else:
            self.tmp_dir = tempfile.TemporaryDirectory(prefix="sonder-cdp-")
            self.user_data_dir = self.tmp_dir.name
        self.port = _free_port()
        self.proc: subprocess.Popen[bytes] | None = None
        self.ws: Any = None
        self._msg_id = 0
        self.headless = headless
        self.width = width
        self.height = height
        self.mobile = mobile
        self.console_errors: list[dict[str, Any]] = []
        self.console_warnings: list[dict[str, Any]] = []
        self.network: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}

    @classmethod
    def attach(cls, port: int, *, user_data_dir: str = "") -> "Chrome":
        """Attach to an already-running Chrome (started by PowerShell)."""
        obj = cls.__new__(cls)
        obj.chrome_path = DEFAULT_CHROME
        obj.tmp_dir = None
        obj.user_data_dir = user_data_dir
        obj.port = int(port)
        obj.proc = None
        obj.ws = None
        obj._msg_id = 0
        obj.headless = True
        obj.width = 1440
        obj.height = 900
        obj.mobile = False
        obj.console_errors = []
        obj.console_warnings = []
        obj.network = []
        obj._handlers = {}
        obj._connect_page()
        return obj

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "Chrome":
        """Launch Chrome directly (works outside the DSH job object; attach is preferred)."""
        args = [
            self.chrome_path,
            "--headless=new",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-gpu",
            "--hide-scrollbars",
            "--mute-audio",
            f"--window-size={self.width},{self.height}",
            "about:blank",
        ]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        last_error = ""
        while time.time() < deadline:
            try:
                info = httpx.get(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=1, trust_env=False
                ).json()
                if info.get("webSocketDebuggerUrl"):
                    break
            except Exception as exc:  # noqa: BLE001 - Chrome still starting
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.3)
        else:
            raise CDPError(f"Chrome start timed out: {last_error}")
        self._connect_page()
        return self

    def _connect_page(self) -> None:
        deadline = time.time() + 30
        page: dict[str, Any] | None = None
        while time.time() < deadline:
            try:
                pages = httpx.get(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=1, trust_env=False
                ).json()
            except Exception:  # noqa: BLE001
                pages = []
            page = next((p for p in pages if p.get("type") == "page"), None)
            if page:
                break
            time.sleep(0.3)
        if not page:
            raise CDPError("no page target available")
        self.ws = ws_client.connect(
            page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024, open_timeout=10
        )
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Network.enable")
        self.call("Log.enable")
        self.install_js_error_collector()

    def install_js_error_collector(self) -> None:
        self.eval(
            "window.__seErrors = [];"
            "window.addEventListener('error', e => window.__seErrors.push(String(e.message||e.error)));"
            "window.addEventListener('unhandledrejection', e => window.__seErrors.push('promise:'+String(e.reason)));"
            "true"
        )

    def close(self, *, keep_profile: bool = False) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
                self.ws = None
        except Exception:  # noqa: BLE001
            pass
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self.tmp_dir is not None and not keep_profile:
            self.tmp_dir.cleanup()
            self.tmp_dir = None

    # ------------------------------------------------------------------ CDP
    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 90.0
    ) -> dict[str, Any]:
        if self.ws is None:
            raise CDPError("not connected")
        self._msg_id += 1
        msg_id = self._msg_id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.recv(timeout=2)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                raise CDPError(f"CDP connection lost: {exc}") from exc
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("id") == msg_id:
                if "error" in message:
                    raise CDPError(f"{method} failed: {message['error']}")
                return message.get("result", {})
            self._handle_event(message)
        raise CDPError(f"{method} timeout")

    def _handle_event(self, message: dict[str, Any]) -> None:
        method = message.get("method") or ""
        params = message.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            kind = str(params.get("type") or "")
            text = " ".join(
                str(arg.get("value", arg.get("description", "")))
                for arg in (params.get("args") or [])
            )[:2000]
            item = {"type": kind, "text": text, "at": time.time()}
            if kind in ("error", "assert"):
                self.console_errors.append(item)
            elif kind in ("warning",):
                self.console_warnings.append(item)
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            level = str(entry.get("level") or "")
            item = {
                "source": entry.get("source"),
                "level": level,
                "text": str(entry.get("text") or "")[:2000],
            }
            if level == "error":
                self.console_errors.append(item)
            elif level == "warning":
                self.console_warnings.append(item)
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails") or {}
            self.console_errors.append(
                {
                    "type": "exception",
                    "text": str(detail.get("text") or "")
                    + " "
                    + str((detail.get("exception") or {}).get("description") or ""),
                }
            )
        elif method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            self.network.append(
                {
                    "id": params.get("requestId"),
                    "url": str(req.get("url") or ""),
                    "method": str(req.get("method") or ""),
                    "status": None,
                    "type": params.get("type"),
                }
            )
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            for item in reversed(self.network):
                if item["id"] == params.get("requestId"):
                    item["status"] = int(response.get("status") or 0)
                    item["mime"] = response.get("mimeType")
                    break
        elif method == "Network.loadingFailed":
            for item in reversed(self.network):
                if item["id"] == params.get("requestId"):
                    item["status"] = -1
                    item["failure"] = str(params.get("errorText") or "")
                    break
        handler = self._handlers.get(method)
        if handler is not None:
            handler(params)

    # ------------------------------------------------------------- shortcuts
    def eval(self, expression: str, *, await_promise: bool = True) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise CDPError(
                f"JS exception: {detail.get('text')} "
                f"{(detail.get('exception') or {}).get('description','')}"[:800]
            )
        return (result.get("result") or {}).get("value")

    def wait_js(self, expression: str, *, timeout: float = 30.0, interval: float = 0.25) -> Any:
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            try:
                last = self.eval(expression)
            except CDPError:
                last = None
            if last:
                return last
            time.sleep(interval)
        raise CDPError(f"wait_js timeout: {expression[:200]} (last={last!r})")

    def navigate(self, url: str) -> None:
        # 先打一个仅存在于旧文档的内存标记：用它判断新文档已经真正加载，
        # 否则 reload 后立刻查 DOM 会拿到旧页面（导致「引导还在」的假失败）。
        try:
            self.eval("window.__cdpOldDoc = true; true")
        except CDPError:
            pass
        self.call("Page.navigate", {"url": url})
        self.wait_js("typeof window.__cdpOldDoc === 'undefined'", timeout=60)
        # SPA 首屏资源较多（音频/图片预热），readyState=complete 可能很慢；
        # 只要文档不再 loading 即交给调用方的 wait_js 做业务断言。
        self.wait_js(
            "document.readyState === 'complete' || document.readyState === 'interactive'",
            timeout=60,
        )

    def screenshot(self, path: str) -> str:
        result = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        data = result.get("data")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            import base64  # noqa: PLC0415

            fh.write(base64.b64decode(data or ""))
        return path

    def set_viewport(self, width: int, height: int, *, mobile: bool = False, scale: int = 1) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": scale,
                "mobile": mobile,
                "screenWidth": width,
                "screenHeight": height,
            },
        )

    def set_offline(self, offline: bool) -> None:
        self.call(
            "Network.emulateNetworkConditions",
            {
                "offline": offline,
                "latency": 0,
                "downloadThroughput": -1 if offline else 10 * 1024 * 1024,
                "uploadThroughput": -1 if offline else 10 * 1024 * 1024,
            },
        )

    # -------------------------------------------------------------- DOM actions
    def click_text(self, text: str, *, tag: str = "button", timeout: float = 15.0) -> bool:
        expr = (
            "(() => { const nodes = [...document.querySelectorAll(%r)];"
            " const hit = nodes.find(n => (n.innerText||'').trim().includes(%r));"
            " if (!hit) return false; hit.scrollIntoView({block:'center'}); hit.click(); return true; })()"
            % (tag, text)
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expr):
                return True
            time.sleep(0.3)
        raise CDPError(f"clickable element not found: <{tag}> contains {text!r}")

    def click_selector(self, selector: str, *, timeout: float = 15.0) -> bool:
        expr = (
            "(() => { const el = document.querySelector(%r); if (!el) return false;"
            " el.scrollIntoView({block:'center'}); el.click(); return true; })()" % selector
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expr):
                return True
            time.sleep(0.3)
        raise CDPError(f"selector not clickable: {selector}")

    def set_value(self, selector: str, value: str) -> bool:
        """React controlled inputs need the native setter plus input/change events."""
        expr = (
            "(() => { const el = document.querySelector(%r); if (!el) return false;"
            " const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
            " const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
            " setter.call(el, %r);"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
            " return true; })()" % (selector, value)
        )
        return bool(self.eval(expr))

    def select_option(self, selector: str, value: str) -> bool:
        expr = (
            "(() => { const el = document.querySelector(%r); if (!el) return false;"
            " el.value = %r; el.dispatchEvent(new Event('change', {bubbles:true}));"
            " el.dispatchEvent(new Event('input', {bubbles:true})); return true; })()"
            % (selector, value)
        )
        return bool(self.eval(expr))

    def text(self) -> str:
        return str(self.eval("document.body ? document.body.innerText : ''") or "")

    def has_text(self, text: str) -> bool:
        return text in self.text()

    def wait_text(self, text: str, *, timeout: float = 30.0) -> None:
        self.wait_js(
            f"document.body && document.body.innerText.includes({json.dumps(text)})",
            timeout=timeout,
        )

    # ---------------------------------------------------------------- report
    def http_errors(self) -> list[dict[str, Any]]:
        bad = []
        for item in self.network:
            status = item.get("status")
            if status is None:
                continue
            if status == -1 or status >= 400:
                url = item.get("url") or ""
                if "/favicon" in url:
                    continue
                bad.append(item)
        return bad

    def js_errors(self, *, ignore: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        collected = list(self.console_errors)
        try:
            page_errors = self.eval("JSON.stringify(window.__seErrors || [])")
            for item in json.loads(page_errors or "[]"):
                collected.append({"type": "window", "text": str(item)})
        except Exception:  # noqa: BLE001
            pass
        if ignore:
            collected = [
                item for item in collected if not any(token in str(item.get("text") or "") for token in ignore)
            ]
        return collected

    def clear_logs(self) -> None:
        self.console_errors.clear()
        self.console_warnings.clear()
        self.network.clear()
        try:
            self.eval("window.__seErrors = []; true")
        except Exception:  # noqa: BLE001
            pass


def cleanup_profile(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


__all__ = ["CDPError", "Chrome", "DEFAULT_CHROME", "cleanup_profile"]
