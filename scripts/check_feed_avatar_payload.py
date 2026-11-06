import http.client
import json


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=20)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body) if body else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data) if data else {}


status, reg = req("POST", "/api/v3/auth/register", {"device_key": "devtest-avatar-payload"})
headers = {"X-User-Id": reg["user_id"], "X-User-Token": reg["token"]}
status, feed = req("GET", "/api/v3/feed?limit=2", headers=headers)
item = feed["items"][0]
author = item["author"]
print("status:", status)
print("author keys:", sorted(author.keys()))
print("avatar:", author.get("avatar"))
print("avatar_seed:", author.get("avatar_seed"))
print("avatar_style:", author.get("avatar_style"))
print("avatar_options:", json.dumps(author.get("avatar_options"), ensure_ascii=False)[:300])
