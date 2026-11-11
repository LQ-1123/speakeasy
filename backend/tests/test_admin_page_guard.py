"""管理台页面守卫：/admin 与 /admin/legacy 的登录保护。

核心回归点：``main.py`` 末尾把整个 Next 静态产物挂在 ``/``（``html=True``），
所以 ``static/app/admin/index.html`` 本可被 ``GET /admin/index.html``
直接取走——那等于绕过登录。本文件的用例就是钉死这个洞。

针对 FastAPI 的路由/mount 优先级，这里用**真实注册顺序**构造应用
（先 include page_router，再 mount 静态目录），而不是简化成只挂 router，
否则测不出真实行为。
"""

from __future__ import annotations

import os
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import api_public_router, page_router, router

ADMIN_INDEX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "static",
    "app",
    "admin",
    "index.html",
)
APP_DIR = os.path.dirname(ADMIN_INDEX)


def _app() -> FastAPI:
    """按 main.py 的真实顺序装配：API → page_router → 静态挂载。"""
    app = FastAPI()
    app.include_router(router)
    app.include_router(api_public_router)
    app.include_router(page_router)
    if os.path.isdir(APP_DIR):
        app.mount("/", StaticFiles(directory=APP_DIR, html=True), name="frontend")
    return app


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("SE_ADMIN_TOKEN", "tok-guard")
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-guard")
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)
    auth._FAILS.clear()
    yield
    auth._FAILS.clear()


@pytest.fixture()
def unconfigured(monkeypatch):
    """未配口令/token：回环直通（本机调试形态）。"""
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    auth._FAILS.clear()
    yield
    auth._FAILS.clear()


def _login(client: TestClient) -> None:
    response = client.post(
        "/admin/login",
        data={"password": "pw-guard", "next": "/admin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert auth.COOKIE_NAME in response.cookies or auth.COOKIE_NAME in client.cookies


def _is_console(html: str) -> bool:
    """是否是新管理台的产物（而不是登录页/旧台/静态兜底）。"""
    return "<div id=\"__next\"" in html or "_next/static" in html


def _is_login(html: str) -> bool:
    return "/admin/login" in html and "管理密码" in html


# --------------------------------------------------------------------------- #
# 未登录：所有入口都必须落到登录页，绝不能吐出管理台
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/admin", "/admin/", "/admin/index.html", "/admin/deep/link"])
def test_unauthenticated_never_returns_console(configured, path):
    """未登录访问任何 /admin 变体，都不能拿到管理台 HTML。

    ``/admin/index.html`` 是最危险的：静态挂载会绕过一切，必须被路由挡住。
    """
    client = TestClient(_app())
    response = client.get(path)
    assert response.status_code == 200
    assert _is_console(response.text) is False, f"{path} 泄漏了管理台内容"
    assert _is_login(response.text) is True, f"{path} 应返回登录页"


def test_unauthenticated_root_static_still_served(configured):
    """守卫只拦 /admin，不误伤客户端首页。"""
    if not os.path.exists(os.path.join(APP_DIR, "index.html")):
        pytest.skip("前端产物未构建")
    client = TestClient(_app())
    response = client.get("/")
    assert response.status_code == 200
    assert _is_login(response.text) is False


# --------------------------------------------------------------------------- #
# 登录后：拿到管理台
# --------------------------------------------------------------------------- #


def test_login_then_admin_returns_console(configured):
    if not os.path.exists(ADMIN_INDEX):
        pytest.skip("管理台前端产物未构建")
    client = TestClient(_app())
    _login(client)
    response = client.get("/admin")
    assert response.status_code == 200
    assert _is_console(response.text) is True


def test_token_header_reaches_console(configured):
    """旧 ?token= / X-Admin-Token 通道保持可用（脚本与远程场景）。"""
    if not os.path.exists(ADMIN_INDEX):
        pytest.skip("管理台前端产物未构建")
    client = TestClient(_app())
    response = client.get("/admin?token=tok-guard")
    assert response.status_code == 200
    assert _is_console(response.text) is True


# --------------------------------------------------------------------------- #
# 旧台回退通道
# --------------------------------------------------------------------------- #


def test_legacy_still_requires_login(configured):
    """``/admin/legacy`` 现在只是普通深链接，但**仍然必须登录**。

    路由删了不等于路径空出来了：``/admin/{path:path}`` 会接住它并返回管理台。
    如果哪天这个 catch-all 忘了鉴权，``/admin/anything`` 就成了绕过入口。
    """
    client = TestClient(_app())
    response = client.get("/admin/legacy")
    assert response.status_code == 200
    assert _is_login(response.text) is True


def test_old_admin_page_is_gone(configured):
    """第 4 期删除旧台：``/admin/legacy`` 不该再出现旧页面的任何痕迹。

    旧页面的稳定特征是「词库管理」标题与 ``legacy`` 那块提示。
    它现在还返回 200（catch-all 吐新控制台），但**内容必须是新控制台**。
    """
    client = TestClient(_app())
    _login(client)
    response = client.get("/admin/legacy")
    assert response.status_code == 200
    assert "词库管理" not in response.text, "旧管理台页面还在（page.py 没删干净？）"
    # 新控制台的容器（静态产物存在时是 SPA，不存在时是"未构建"提示）
    assert _is_console(response.text) is True or "管理台前端尚未构建" in response.text


def test_legacy_page_py_file_removed():
    """直接钉住文件本身：``page.py`` 必须已删除，防止被误恢复。"""
    from app.admin import __file__ as admin_pkg  # noqa: PLC0415

    page_py = pathlib.Path(admin_pkg).parent / "page.py"
    assert not page_py.exists(), "backend/app/admin/page.py 应该已删除"


# --------------------------------------------------------------------------- #
# 未配口令：本机直通
# --------------------------------------------------------------------------- #


def test_loopback_passthrough_when_unconfigured(unconfigured):
    """未配口令时，回环地址直接进管理台（本机调试形态保持不变）。"""
    if not os.path.exists(ADMIN_INDEX):
        pytest.skip("管理台前端产物未构建")
    client = TestClient(_app())
    response = client.get("/admin")
    assert response.status_code == 200
    assert _is_console(response.text) is True


def test_loopback_legacy_passthrough_when_unconfigured(unconfigured):
    client = TestClient(_app())
    response = client.get("/admin/legacy")
    assert response.status_code == 200
    # 旧台已删：未配口令时 /admin/legacy 走的是新控制台的 catch-all，
    # 不该再出现旧页面内容。
    assert "词库管理" not in response.text


# --------------------------------------------------------------------------- #
# 登录页本身
# --------------------------------------------------------------------------- #


def test_login_page_is_sonder_branded(configured):
    client = TestClient(_app())
    html = client.get("/admin").text
    assert "Sonder" in html
    assert "管理密码" in html
    assert "noindex" in html
    # 旧品牌不应再出现
    assert "SpeakEasy" not in html


def test_login_page_has_no_legacy_link(configured):
    """登录页不该再引导去旧台——旧台已删，链接只会让人以为还有回退通道。"""
    client = TestClient(_app())
    html = client.get("/admin").text
    assert "/admin/legacy" not in html


def test_logout_clears_cookie(configured):
    client = TestClient(_app())
    _login(client)
    assert client.get("/admin").status_code == 200
    response = client.post("/admin/logout", follow_redirects=False)
    assert response.status_code == 303
    # 退出后回到登录页
    after = client.get("/admin")
    assert _is_login(after.text) is True


def test_missing_frontend_build_gives_actionable_message(configured, monkeypatch):
    """前端产物缺失时给可操作提示，而不是 500 或误判为未登录。"""
    import importlib

    # 注意：`from app.admin import router` 拿到的是 APIRouter 对象（`app.admin.__init__`
    # 把同名符号重导出），要改的是**模块**，必须按完整路径 import。
    module = importlib.import_module("app.admin.router")
    monkeypatch.setattr(
        module, "_admin_index_path", lambda: "D:/nonexistent/admin/index.html"
    )
    client = TestClient(_app())
    _login(client)
    response = client.get("/admin")
    assert response.status_code == 503
    assert "npm run build:backend" in response.text
