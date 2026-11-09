"""验收脚本：管理端（词库）热生效 / 校验拦截 / 回滚。

    python scripts/check_admin.py

只碰本机管理接口（默认仅 127.0.0.1 可访问），改完会回滚，不留副作用。
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        page = client.get("/admin")
        print(f"① 管理页: {page.status_code}，{len(page.text)} 字节")
        status = client.get("/api/admin/status").json()
        print("② 已生效:", json.dumps(status["applied"], ensure_ascii=False))
        print("   词库:", ", ".join(f"{i['name']}={'ok' if i['ok'] else 'bad'}" for i in status["lexicons"]))

        dna = client.get("/api/admin/lexicons/dna").json()
        data = dna["data"]

        # ③ 校验拦截：非法数值必须被拒绝
        bad = json.loads(json.dumps(data))
        bad["fields"]["verbosity"]["values"]["long"]["trait"] = 2.0
        res = client.put("/api/admin/lexicons/dna", json={"data": bad}).json()
        print("③ 非法保存被拒绝:", not res.get("ok"), "|", (res.get("issues") or [""])[0][:80])

        # ④ 合法保存 → 热生效
        good = json.loads(json.dumps(data))
        old = float(good["fields"]["verbosity"]["values"]["long"]["trait"])
        good["fields"]["verbosity"]["values"]["long"]["trait"] = 0.51
        res = client.put("/api/admin/lexicons/dna", json={"data": good}).json()
        print("④ 保存:", res.get("ok"), "| 备份:", os.path.basename(res.get("backup") or "-"))
        print("   生效:", json.dumps(res.get("applied"), ensure_ascii=False))

        # ⑤ 回滚上一版
        reset = client.post("/api/admin/lexicons/dna/reset").json()
        again = client.get("/api/admin/lexicons/dna").json()
        restored = float(again["data"]["fields"]["verbosity"]["values"]["long"]["trait"])
        print(f"⑤ 回滚: {reset.get('restored')} | trait {old} → 0.51 → {restored}",
              "OK" if abs(restored - old) < 1e-9 else "FAIL")

        usage = client.get("/api/admin/usage").json()
        print(f"⑥ 使用统计: {usage.get('personas')} 个角色有 DNA，"
              f"场景覆盖 {len(usage.get('scenes') or {})} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
