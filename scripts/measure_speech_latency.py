"""测量朗读接口的响应延迟（服务端视角，毫秒）。"""

import http.client
import time

POSTS = ["pp_jiwoo_24", "pp_adrian_02", "pp_lin_27", "pp_giulia_15"]


def measure(pid: str) -> tuple[float, int, str, str]:
    c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=60)
    t0 = time.perf_counter()
    c.request("GET", f"/api/v3/persona-posts/{pid}/speech")
    r = c.getresponse()
    data = r.read()
    dt = (time.perf_counter() - t0) * 1000
    c.close()
    return dt, len(data), r.getheader("X-Audio-Cached") or "-", r.getheader("Content-Type") or "-"


for pid in POSTS:
    dt, size, cached, ctype = measure(pid)
    print(f"{pid:16s} {dt:7.1f} ms  {size/1024:6.1f} KB  cached={cached}  {ctype}")
