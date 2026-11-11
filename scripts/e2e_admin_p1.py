"""P1 端到端验收：真实 HTTP 走一遍管理台主流程。

与 pytest 不同，这里打的是**真实启动的 uvicorn**，验证的是
"路由挂载顺序 / 静态产物 / Cookie / CSRF / 审计落库"这些
只有在完整应用里才会暴露的问题。

用法：
    python scripts/e2e_admin_p1.py [base_url]

默认读 SE_E2E_BASE（缺省 http://127.0.0.1:8899）与 SE_E2E_PASSWORD。
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.getenv("SE_E2E_BASE", "http://127.0.0.1:8899")
PASSWORD = os.getenv("SE_E2E_PASSWORD", "test-pw-123456")
TOKEN = os.getenv("SE_ADMIN_TOKEN", "test-token-abc")

# 本机注册了系统代理；本脚本只打回环，必须绕开代理，否则 502。
CLIENT_KW = {"base_url": BASE, "timeout": 60.0, "trust_env": False}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def is_console(html: str) -> bool:
    return "_next/static" in html or '__next' in html


def is_login(html: str) -> bool:
    return "/admin/login" in html and "管理密码" in html


def main() -> int:
    with httpx.Client(**CLIENT_KW) as client:
        # ---------- 1. 未登录：所有入口都不能泄漏管理台 ----------
        for path in ("/admin", "/admin/", "/admin/index.html", "/admin/deep"):
            r = client.get(path)
            check(
                f"未登录 {path} → 登录页（不泄漏控制台）",
                r.status_code == 200 and is_login(r.text) and not is_console(r.text),
                f"{r.status_code}",
            )

        r = client.get("/api/admin/status")
        check("未登录 API → 403", r.status_code == 403, str(r.status_code))

        r = client.get("/api/admin/session")
        session = r.json() if r.status_code == 200 else {}
        check(
            "未登录 /session → 200 且 authenticated=false",
            r.status_code == 200 and session.get("authenticated") is False,
            json.dumps(session, ensure_ascii=False)[:90],
        )

        # ---------- 2. 登录 ----------
        r = client.post(
            "/admin/login",
            data={"password": PASSWORD, "next": "/admin"},
            follow_redirects=False,
        )
        check("登录 → 303 + Cookie", r.status_code == 303 and "se_admin_session" in r.cookies, str(r.status_code))

        # ---------- 3. 登录后拿到控制台 ----------
        r = client.get("/admin")
        check("登录后 /admin → 控制台", r.status_code == 200 and is_console(r.text), f"{len(r.text)}B")

        r = client.get("/admin/index.html")
        check(
            "登录后 /admin/index.html → 控制台（不被静态绕过也不漏给未登录）",
            r.status_code == 200 and is_console(r.text),
        )

        # ---------- 4. 旧台已删除（P4 收尾） ----------
        r = client.get("/admin/legacy")
        check(
            "第 4 期：/admin/legacy 不再返回旧页面",
            r.status_code == 200 and "词库管理" not in r.text,
            f"{len(r.text)}B",
        )

        # ---------- 5. session / CSRF ----------
        r = client.get("/api/admin/session")
        session = r.json()
        csrf = session.get("csrf") or ""
        check(
            "登录后 /session → csrf 非空",
            session.get("authenticated") is True and len(csrf) >= 20,
            f"csrf={csrf[:12]}…",
        )

        # ---------- 6. CSRF：P4 起全强制（不再有灰名单） ----------
        # 历史：P1~P3 为兼容旧 page.py 曾用「带就校验、不带放行」的灰名单；
        # P4 删除旧台后收口为「Cookie 身份的写请求一律要求 CSRF」。
        #   Cookie 身份 + 无 CSRF 头 → 403
        #   Cookie 身份 + 错 CSRF   → 403
        #   Cookie 身份 + 对 CSRF   → 通过
        #   X-Admin-Token（脚本）   → 豁免（自定义头不构成 CSRF 面）
        #   跨站来源                → 一律 403
        payload = {"data": {"fields": {}}}
        r = client.put("/api/admin/lexicons/dna", json=payload)
        check(
            "不带 CSRF 头 → 403（P4 全强制）",
            r.status_code == 403,
            str(r.status_code),
        )

        r = client.put(
            "/api/admin/lexicons/dna",
            json=payload,
            headers={"X-Admin-CSRF": "wrong-value"},
        )
        check("带错误 CSRF 值 → 403", r.status_code == 403, str(r.status_code))

        r = client.put(
            "/api/admin/lexicons/dna",
            json=payload,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        check("跨站来源的写请求 → 403", r.status_code == 403, str(r.status_code))

        r = client.put(
            "/api/admin/lexicons/dna",
            json=payload,
            headers={"X-Admin-CSRF": csrf},
        )
        check(
            "带正确 CSRF → 放行（dna 校验失败返回业务错误，非 403）",
            r.status_code != 403,
            f"{r.status_code}",
        )

        # ---------- 7. 总览 ----------
        r = client.get("/api/admin/overview")
        ok = r.status_code == 200
        data = r.json() if ok else {}
        keys = {"users", "personas", "today", "services", "jobs", "push"}
        check(
            "GET /overview 结构完整",
            ok and keys.issubset(set(data)),
            ",".join(sorted(keys - set(data))) or "ok",
        )
        if ok:
            check(
                "overview.services 含五个服务",
                {"llm", "stt", "tts", "deepgram", "jpush"}.issubset(set(data.get("services") or {})),
                ",".join(sorted((data.get("services") or {}).keys())),
            )
            print(
                "      overview 实测: "
                f"users={data['users']} personas={data['personas']} today={data['today']}"
            )

        # ---------- 8. 后台任务 ----------
        r = client.get("/api/admin/jobs")
        jobs = r.json() if r.status_code == 200 else {}
        check(
            "GET /jobs 结构完整",
            r.status_code == 200 and {"items", "total", "offset", "limit"}.issubset(set(jobs)),
            f"total={jobs.get('total')}",
        )

        r = client.get("/api/admin/jobs/kinds")
        kinds = (r.json() or {}).get("kinds") if r.status_code == 200 else None
        check(
            "GET /jobs/kinds 返回 11 种任务",
            isinstance(kinds, list) and len(kinds) >= 11,
            f"{len(kinds or [])} 种",
        )

        r = client.post(
            "/api/admin/jobs/cleanup/run",
            headers={"X-Admin-CSRF": csrf},
        )
        check(
            "POST /jobs/cleanup/run 入队成功",
            r.status_code == 200 and (r.json() or {}).get("ok") is True,
            json.dumps(r.json(), ensure_ascii=False)[:70] if r.status_code == 200 else str(r.status_code),
        )

        r = client.post(
            "/api/admin/jobs/not-a-kind/run",
            headers={"X-Admin-CSRF": csrf},
        )
        check("未知 kind → 422", r.status_code == 422, str(r.status_code))

        # ---------- 9. 旧 API 仍然可用 ----------
        for path in ("/api/admin/status", "/api/admin/lexicons", "/api/admin/personas"):
            r = client.get(path)
            check(f"旧接口 {path} 仍 200", r.status_code == 200, str(r.status_code))

        # ---------- 10. Token 头豁免 CSRF ----------
        with httpx.Client(**CLIENT_KW) as token_client:
            r = token_client.put(
                "/api/admin/lexicons/dna",
                json=payload,
                headers={"X-Admin-Token": TOKEN},
            )
            check(
                "X-Admin-Token 头豁免 CSRF（非 403）",
                r.status_code != 403,
                str(r.status_code),
            )

        # ---------- 11. 退出 ----------
        r = client.post("/admin/logout", follow_redirects=False)
        check("退出 → 303", r.status_code == 303, str(r.status_code))
        r = client.get("/admin")
        check("退出后 /admin → 登录页", is_login(r.text) and not is_console(r.text))

    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
