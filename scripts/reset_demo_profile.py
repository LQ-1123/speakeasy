"""把资料恢复成演示默认：淮安 / 西安 / 女 / 兴趣。"""

import http.client
import json


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=20)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    c.request(method, path, body=payload, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data) if data else {}


_, reg = req("POST", "/api/v3/auth/register", {"device_key": "demo-profile-reset"})
headers = {"X-User-Id": reg["user_id"], "X-User-Token": reg["token"]}
status, res = req(
    "PUT",
    "/api/v3/profile",
    {
        "nickname": "淮安",
        "gender": "female",
        "city": "西安",
        "job": "软件工程大学生",
        "interests": ["咖啡", "旅行", "摄影", "读书"],
    },
    headers,
)
print("reset:", status, res.get("profile", {}).get("nickname"))
