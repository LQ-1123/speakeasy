import http.client
import json
import os


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=20)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body) if body else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return json.loads(data) if data else {}


reg = req("POST", "/api/v3/auth/register", {"device_key": "devtest-render-debug"})
headers = {"X-User-Id": reg["user_id"], "X-User-Token": reg["token"]}
feed = req("GET", "/api/v3/feed?limit=4", headers=headers)
out = []
for item in feed["items"][:4]:
    a = item["author"]
    out.append({"id": a["id"], "seed": a["avatar_seed"], "options": a.get("avatar_options")})
os.makedirs("docs/avatar-lab", exist_ok=True)
path = "docs/avatar-lab/render-debug.json"
with open(path, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
print("wrote", path, len(out))
