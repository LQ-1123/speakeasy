"""P4 端到端：鉴权收口 + 操作日志真的在落库。

为什么单独一个脚本
------------------
P4 做的是**语义收紧**：
- CSRF 从「灰名单」变「全强制」——Cookie 身份的写请求不带 `X-Admin-CSRF` 一律 403；
- 审计从「字段设了但没人写」变「每次成功的写操作都落一行 `admin_audit`」；
- 旧管理台 `page.py` / `/admin/legacy` 删除。

前两件事都属于「不做就悄悄失效」的类型：CSRF 弱了不会报错，审计没落库也不会报错，
只是等出事时才发现没有记录。所以必须在真服务上验证一遍。
"""

from __future__ import annotations

import os
import pathlib
import sys

import httpx

BASE = os.environ.get("SE_E2E_BASE", "http://127.0.0.1:8899")
PASSWORD = os.environ.get("SE_ADMIN_PASSWORD", "test-pw-123456")
TOKEN = os.environ.get("SE_ADMIN_TOKEN", "test-token-abc")
ROOT = pathlib.Path(__file__).resolve().parent.parent

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main() -> int:
    # ---------------- 静态：旧文件必须真的删了 ----------------
    page_py = ROOT / "backend" / "app" / "admin" / "page.py"
    check(not page_py.exists(), "旧管理台 page.py 已删除", str(page_py))

    with httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False, timeout=60.0) as c:
        # ---------------- 登录 ----------------
        resp = c.post("/admin/login", data={"password": PASSWORD, "next": "/admin"})
        check(resp.status_code in (303, 302), "管理台登录", f"HTTP {resp.status_code}")
        if resp.status_code not in (303, 302):
            return 1

        session = c.get("/api/admin/session").json()
        csrf = str(session.get("csrf") or "")
        check(bool(csrf), "拿到 CSRF 令牌", csrf[:8] + "…")
        check(session.get("authenticated") is True, "会话已认证")

        # ---------------- CSRF 全强制 ----------------
        # 用一个必定成功的写请求做探针：清空某个角色的 Prompt 改写（幂等、无副作用）。
        personas = c.get("/api/admin/personas?limit=3").json()
        items = personas.get("personas") or personas.get("items") or []
        check(bool(items), "能取到角色", f"{len(items)} 个")
        if not items:
            return 1
        pid = str(items[0].get("id") or "")

        write_path = "/api/admin/prompt/overrides"
        no_csrf = c.put(write_path, json={"persona_id": pid, "mode": "text", "modules": {}})
        check(
            no_csrf.status_code == 403,
            "Cookie 写请求不带 CSRF → 403（P4 全强制）",
            f"HTTP {no_csrf.status_code}",
        )

        bad_csrf = c.put(
            write_path,
            json={"persona_id": pid, "mode": "text", "modules": {}},
            headers={"X-Admin-CSRF": "definitely-wrong"},
        )
        check(bad_csrf.status_code == 403, "CSRF 错误 → 403", f"HTTP {bad_csrf.status_code}")

        good = c.put(
            write_path,
            json={"persona_id": pid, "mode": "text", "modules": {}},
            headers={"X-Admin-CSRF": csrf},
        )
        check(good.status_code == 200, "CSRF 正确 → 200", f"HTTP {good.status_code}")

        # 脚本通道（X-Admin-Token）不带 CSRF 也必须能用：自定义头不构成 CSRF 面
        token_write = c.put(
            write_path,
            json={"persona_id": pid, "mode": "text", "modules": {}},
            headers={"X-Admin-Token": TOKEN},
        )
        check(
            token_write.status_code == 200,
            "token 通道免 CSRF 仍可用（脚本/CLI 不被破坏）",
            f"HTTP {token_write.status_code}",
        )

        # ---------------- 操作日志：真的落库了 ----------------
        # 先记下当前日志条数，再做一次写操作，再确认多了一条。
        before = c.get("/api/admin/audit?limit=1").json()
        check(before.get("ok") is True, "GET /audit 可用", str(before.get("issues") or ""))
        before_total = int(before.get("total") or 0)

        # 一次可识别的写：给某个角色保存一个模块改写，然后清掉
        marker_ok = c.put(
            write_path,
            json={"persona_id": pid, "mode": "text", "modules": {"character_card": "E2E 审计探针"}},
            headers={"X-Admin-CSRF": csrf},
        ).json()
        check(marker_ok.get("ok") is True, "写入一条可识别的改动", str(marker_ok.get("issues") or ""))

        after = c.get("/api/admin/audit?limit=50").json()
        after_total = int(after.get("total") or 0)
        check(
            after_total > before_total,
            "写操作后操作日志条数增加",
            f"{before_total} → {after_total}",
        )

        rows = after.get("items") or []
        check(bool(rows), "日志列表非空", f"{len(rows)} 条")
        if rows:
            # 注意：不能假设 rows[0] 就是刚写的那条——同一秒内可能有别的写入，
            # 且服务可能已被其它脚本写过。在最近若干条里找「含本角色 id」的那条。
            target_text = ""
            for row in rows[:20]:
                candidate = str(row.get("target") or "")
                if pid in candidate:
                    target_text = candidate
                    break
            check(
                bool(target_text),
                "最近记录里能找到刚操作的对象",
                target_text or f"前 20 条都没提到 {pid}",
            )
            latest = rows[0]
            check(bool(latest.get("at")), "记录带时间", str(latest.get("at")))
            check(bool(latest.get("action")), "记录带动作", str(latest.get("action")))
            # 摘要是"字段名"而不是值：不该出现我们刚写进去的正文
            blob = " ".join(str(r.get("summary") or "") for r in rows)
            check(
                "E2E 审计探针" not in blob,
                "摘要不含内容值（只记字段名）",
                "泄漏了正文" if "E2E 审计探针" in blob else "",
            )

        actions = c.get("/api/admin/audit/actions").json()
        check(actions.get("ok") is True, "GET /audit/actions 可用")
        check(bool(actions.get("actions")), "动作清单非空", str(len(actions.get("actions") or [])))

        # 按动作筛选
        first_action = str((actions.get("actions") or [{}])[0].get("action") or "")
        if first_action:
            filtered = c.get("/api/admin/audit", params={"action": first_action}).json()
            only = {str(r.get("action")) for r in (filtered.get("items") or [])}
            check(only <= {first_action}, "按动作筛选只返回该动作", str(sorted(only)))

        # ---------------- 未登录仍需 403，但 /session 可访问 ----------------
        fresh = httpx.Client(base_url=BASE, trust_env=False, timeout=30.0)
        try:
            probe = fresh.get("/api/admin/status")
            check(probe.status_code == 403, "未登录访问管理 API → 403", f"HTTP {probe.status_code}")
            sess = fresh.get("/api/admin/session")
            check(
                sess.status_code == 200 and sess.json().get("authenticated") is False,
                "未登录 /session 仍 200（前端靠它决定跳登录页）",
                f"HTTP {sess.status_code}",
            )
            out = fresh.post("/api/admin/logout")
            check(out.status_code == 200, "未登录调 /logout 仍 200（幂等）", f"HTTP {out.status_code}")
        finally:
            fresh.close()

        # ---------------- 收尾：清掉探针写入 ----------------
        c.request(
            "DELETE",
            f"/api/admin/prompt/overrides?persona_id={pid}&mode=text",
            headers={"X-Admin-CSRF": csrf},
        )

    failed = [r for r in results if not r[0]]
    print(f"\n合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for _ok, name, detail in failed:
            print(f"  - {name}  {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
