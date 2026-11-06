"""验证云端 web：健康、静态版本、feed、朗读音色。"""

import http.client
import json
import time
import urllib.request

BASE = "http://47.104.24.236:8000"


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path)
    r = urllib.request.urlopen(req, timeout=timeout)
    body = r.read()
    return r, body


print("== health ==")
r, body = get("/api/v3/health", timeout=20)
print(r.status, body[:160].decode("utf-8", "replace"))

print("\n== root ==")
r, body = get("/", timeout=20)
html = body.decode("utf-8", "replace")
import re

m = re.search(r"app/page-[a-z0-9]+\.js", html)
print("status", r.status, "| page chunk:", m.group(0) if m else "none", "| bytes", len(body))

print("\n== feed ==")
r, body = get("/api/v3/feed?limit=3", timeout=40)
data = json.loads(body)
items = data.get("items", [])
print("status", r.status, "| items", len(items))
for it in items[:3]:
    print("  -", it.get("kind"), "|", it.get("author", {}).get("nickname"), "|", (it.get("content") or "")[:40])

print("\n== 朗读（首条人物动态） ==")
post = next((it for it in items if it.get("kind") == "persona_post"), None)
if post:
    url = f"/api/v3/persona-posts/{post['id']}/speech?v={post.get('content_hash','')}&a={post.get('speech_version','')}"
    t = time.time()
    try:
        r, body = get(url, timeout=120)
        print("status", r.status, "| voice", r.headers.get("x-voice"), "| lang", r.headers.get("x-language"),
              "| bytes", len(body), "| ms", int((time.time() - t) * 1000))
    except Exception as exc:  # noqa: BLE001
        print("speech failed:", exc)
else:
    print("no persona post")

print("\n== 静态资源 ==")
for path in ["/manifest.webmanifest", "/_next/static/css/", "/api/v3/world/daily"]:
    try:
        r, body = get(path, timeout=20)
        print(path, "->", r.status, len(body))
    except Exception as exc:  # noqa: BLE001
        print(path, "->", exc)
