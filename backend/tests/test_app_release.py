"""APK 发布 / 在线更新元数据测试（不联网、不依赖真实 APK）。"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import app_release
from app.admin.router import router as admin_router


@pytest.fixture()
def release_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_release, "_root", lambda: tmp_path)
    yield tmp_path


@pytest.fixture()
def client(release_root, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(app_release.router)
    app.include_router(admin_router)
    app.state.v2 = SimpleNamespace(engines=None)
    with TestClient(app) as http:
        yield http


def _fake_apk(size: int = 4096) -> bytes:
    return b"PK\x03\x04" + b"\x00" * (size - 4)


def test_latest_empty(client):
    data = client.get("/api/app/latest").json()
    assert data["available"] is False
    assert data["versionCode"] == 0


def test_publish_and_latest_and_download(client):
    res = client.post(
        "/api/admin/app-release/upload",
        files={"file": ("speakeasy.apk", _fake_apk(), "application/vnd.android.package-archive")},
        data={
            "version_code": "301",
            "version_name": "3.0.1",
            "changelog": "消息页改版；语音稳定性修复",
            "force": "false",
            "min_version_code": "300",
        },
    ).json()
    assert res["ok"] is True
    current = res["current"]
    assert current["versionCode"] == 301
    assert current["versionName"] == "3.0.1"
    assert current["force"] is False
    assert current["minVersionCode"] == 300
    assert len(current["sha256"]) == 64

    latest = client.get("/api/app/latest").json()
    assert latest["available"] is True
    assert latest["versionCode"] == 301
    assert latest["apkUrl"].endswith(".apk")

    dl = client.get(latest["apkUrl"])
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("application/vnd.android.package-archive")
    assert dl.content.startswith(b"PK\x03\x04")

    # Range（断点续传）
    part = client.get(latest["apkUrl"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206
    assert len(part.content) == 100


def test_duplicate_version_rejected(client):
    payload = dict(
        files={"file": ("a.apk", _fake_apk(), "application/vnd.android.package-archive")},
        data={"version_code": "301", "version_name": "3.0.1"},
    )
    assert client.post("/api/admin/app-release/upload", **payload).json()["ok"] is True
    second = client.post("/api/admin/app-release/upload", **payload).json()
    assert second["ok"] is False
    assert "已经发布过" in second["issues"][0]


def test_invalid_version_rejected(client):
    res = client.post(
        "/api/admin/app-release/upload",
        files={"file": ("a.apk", _fake_apk(), "application/vnd.android.package-archive")},
        data={"version_code": "0", "version_name": "x"},
    ).json()
    assert res["ok"] is False


def test_rollback_restores_previous(client):
    for code, name in ((301, "3.0.1"), (302, "3.0.2")):
        client.post(
            "/api/admin/app-release/upload",
            files={"file": ("a.apk", _fake_apk(), "application/vnd.android.package-archive")},
            data={"version_code": str(code), "version_name": name},
        )
    assert client.get("/api/app/latest").json()["versionCode"] == 302
    res = client.post("/api/admin/app-release/rollback").json()
    assert res["ok"] is True
    assert res["current"]["versionCode"] == 301
    assert client.get("/api/app/latest").json()["versionCode"] == 301


def test_admin_endpoints_require_token_when_configured(client, monkeypatch):
    monkeypatch.setenv("SE_ADMIN_TOKEN", "secret-123")
    denied = client.get("/api/admin/app-release")
    assert denied.status_code == 403
    ok = client.get("/api/admin/app-release", headers={"X-Admin-Token": "secret-123"})
    assert ok.status_code == 200


def test_settings_fields_public_shape(release_root):
    app_release.publish(
        "x.apk",
        _fake_apk(),
        version_code=310,
        version_name="3.1.0",
        changelog="强制升级测试",
        force=True,
        min_version_code=310,
    )
    info = app_release.latest_info()
    assert info["force"] is True
    assert info["minVersionCode"] == 310
    assert info["size"] == 4096
    assert info["sha256"] == app_release.load_meta()["current"]["sha256"]
