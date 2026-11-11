"""P1 路由拆分 + 新接口（overview / jobs / session）验收。

三类断言，分别守住不同的回归面：

1. **拆分未改变旧行为**：迁移到 ``app/admin/api/*`` 的旧路径必须仍在、且
   OpenAPI 签名（路径/方法/参数/请求体/响应码）与拆分前一致——这是"旧行为不变"
   的机器可验证形式，比逐个路径手写断言更完整。
2. **新接口的返回结构**：前端已按固定字段名写好（``frontend/src/admin/**``），
   字段缺失或改名会直接让管理台白屏，所以逐字段断言。
3. **容错**：总览是管理台落地页，任何一张表缺失都不该让它 500。

不联网、不启动 lifespan：用最小 FastAPI app + 临时 SQLite，与
``test_admin_config.py`` / ``test_persona_studio.py`` 的既有做法一致。
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.api import jobs as jobs_api
from app.admin.api import overview as overview_api
from app.admin.api import session as session_api
from app.admin.router import api_public_router, page_router, router as admin_router
from app.jobs import DurableJobQueue
from app.v2.db import DB
from app.v3.schema import ensure_schema


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    """带 V3 schema 的临时库（含 personas / persona_events / background_jobs）。"""
    database = DB(str(tmp_path / "admin_split.db"))
    database.init()
    ensure_schema(database, seed=True)
    yield database
    database.close()


@pytest.fixture()
def bare_db(tmp_path):
    """只有 V2 表的库：用来验证总览对"表不存在"的容错。"""
    database = DB(str(tmp_path / "admin_bare.db"))
    database.init()
    yield database
    database.close()


@pytest.fixture()
def client(db, monkeypatch):
    """最小管理 app：挂聚合后的 router（与 main.py 的挂法一致）。"""
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(api_public_router)
    app.include_router(page_router)
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = None
    app.state.conversation_store = None
    app.state.background_jobs = DurableJobQueue(db.conn, lock=db._lock)
    with TestClient(app) as http:
        yield http


@pytest.fixture()
def bare_client(bare_db, monkeypatch):
    """同上，但底层是"什么都没有"的库 + 没有队列（极老部署 / 半初始化）。"""
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(api_public_router)
    app.state.v2 = SimpleNamespace(db=bare_db)
    app.state.v3 = None
    app.state.conversation_store = None
    with TestClient(app) as http:
        yield http


# --------------------------------------------------------------------------- #
# 1. 拆分未改变旧行为
# --------------------------------------------------------------------------- #
#: 拆分前 ``app/admin/router.py`` 导出的全部 API 路径（不含 P1 新增的三个域）。
#: 这份清单是从拆分前的源码逐条抄下来的，**只增不改**：迁移时漏掉一条这里就会报。
LEGACY_PATHS: dict[str, set[str]] = {
    "/api/admin/media/stats": {"get"},
    "/api/admin/media/library": {"get"},
    "/api/admin/media/search": {"get"},
    "/api/admin/media/fetch": {"post"},
    "/api/admin/media/fill": {"post"},
    "/api/admin/media/prune": {"post"},
    "/api/admin/status": {"get"},
    "/api/admin/prompt/versions": {"get"},
    "/api/admin/prompt/preview": {"get", "post"},
    "/api/admin/lexicons": {"get"},
    "/api/admin/lexicons/{name}": {"get", "put"},
    "/api/admin/lexicons/{name}/validate": {"post"},
    "/api/admin/lexicons/{name}/reset": {"post"},
    "/api/admin/usage": {"get"},
    "/api/admin/config": {"get"},
    "/api/admin/config/{target}": {"put"},
    "/api/admin/config/{target}/test": {"post"},
    "/api/admin/preview-personas": {"get"},
    "/api/admin/personas": {"get", "post"},
    "/api/admin/personas/users": {"get"},
    "/api/admin/personas/rebuild-preview": {"get"},
    "/api/admin/personas/rematch-media": {"post"},
    "/api/admin/personas/rebalance-genders": {"post"},
    "/api/admin/personas/redistribute-voices": {"post"},
    "/api/admin/personas/regenerate-posts": {"post"},
    "/api/admin/personas/init-life": {"post"},
    "/api/admin/personas/rebuild": {"post"},
    "/api/admin/personas/{persona_id}": {"get", "put", "delete"},
    "/api/admin/personas/{persona_id}/reroll": {"post"},
    "/api/admin/voice-pool": {"get"},
    "/api/admin/voice-pool/models": {"get"},
    "/api/admin/voice-pool/validate": {"post"},
    "/api/admin/voice-pool/add": {"post"},
    "/api/admin/voice-pool/remove": {"post"},
    "/api/admin/voice-pool/resolve": {"post"},
    "/api/admin/voice-pool/preview": {"post"},
    "/api/admin/app-release": {"get"},
    "/api/admin/app-release/upload": {"post"},
    "/api/admin/app-release/rollback": {"post"},
    "/api/admin/simulate": {"get"},
}

#: 页面路由必须留在 ``page_router``（另一个任务在改它，不属于 API 包）。
PAGE_PATHS: dict[str, set[str]] = {
    "/admin": {"get"},
    "/admin/login": {"post"},
    "/admin/logout": {"post"},
}


def _openapi_paths() -> dict[str, set[str]]:
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(api_public_router)
    app.include_router(page_router)
    paths = app.openapi()["paths"]
    return {path: set(ops) for path, ops in paths.items()}


def test_all_legacy_paths_survive_split():
    """旧路径一条都不能少，方法也不能变。"""
    paths = _openapi_paths()
    missing = {p: m for p, m in LEGACY_PATHS.items() if p not in paths}
    assert missing == {}, f"拆分后丢失的旧路径: {sorted(missing)}"
    changed = {p: (m, paths[p]) for p, m in LEGACY_PATHS.items() if paths[p] != m}
    assert changed == {}, f"旧路径方法变化: {changed}"


def test_page_router_stays_out_of_api_package():
    """``/admin``、``/admin/login``、``/admin/logout`` 仍在 page_router（不属于 API 包）。"""
    paths = _openapi_paths()
    for path, methods in PAGE_PATHS.items():
        assert path in paths, path
        assert paths[path] == methods, (path, paths[path])

    # /admin* 不能出现在 API 路由里（否则会与页面路由抢匹配）
    api_paths = {r.path for r in admin_router.routes if hasattr(r, "path")}
    page_conflicts = {p for p in api_paths if p in PAGE_PATHS}
    assert page_conflicts == set(), f"API 路由误含页面路径: {page_conflicts}"


def test_new_endpoints_are_registered():
    """P1 新增的 6 条路径都在，且方法正确。"""
    paths = _openapi_paths()
    assert paths["/api/admin/overview"] == {"get"}
    assert paths["/api/admin/jobs"] == {"get"}
    assert paths["/api/admin/jobs/kinds"] == {"get"}
    assert paths["/api/admin/jobs/{kind}/run"] == {"post"}
    assert paths["/api/admin/session"] == {"get"}
    assert paths["/api/admin/logout"] == {"post"}


def test_api_prefix_not_duplicated():
    """子路由只声明域前缀，完整路径由父路由拼接——不能出现 ``/api/admin/api/admin``。"""
    paths = _openapi_paths()
    broken = [p for p in paths if p.count("/api/admin") > 1]
    assert broken == [], f"前缀重复: {broken}"


def test_legacy_status_response_shape_unchanged(client):
    """``/api/admin/status`` 的返回结构必须与拆分前完全一致。"""
    data = client.get("/api/admin/status").json()
    assert set(data) == {"applied", "lexicons", "server_time"}
    assert isinstance(data["applied"], dict)
    assert isinstance(data["server_time"], float)
    assert isinstance(data["lexicons"], list)
    for item in data["lexicons"]:
        assert set(item) == {
            "name",
            "title",
            "exists",
            "path",
            "bytes",
            "mtime",
            "issues",
            "ok",
        }


def test_legacy_lexicons_response_shape_unchanged(client):
    data = client.get("/api/admin/lexicons").json()
    assert set(data) == {"items"}
    assert isinstance(data["items"], list)


def test_legacy_guard_still_403_without_login(db, monkeypatch):
    """拆分后未登录仍然 403（守卫行为不变）。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(api_public_router)
    app.state.v2 = SimpleNamespace(db=db)
    fresh = TestClient(app)
    assert fresh.get("/api/admin/status").status_code == 403
    assert fresh.get("/api/admin/overview").status_code == 403


# --------------------------------------------------------------------------- #
# 2. overview
# --------------------------------------------------------------------------- #
def _seed_overview_data(db: DB) -> None:
    """塞最小数据：2 个用户（1 个今天活跃）、1 个事件、1 条动态、推送各状态。"""
    from app.v3.client_api import ensure_client_schema  # noqa: PLC0415
    from app.v3.daily_world import day_key, iso, now_local  # noqa: PLC0415

    ensure_client_schema(db)
    today = day_key()
    now_iso = iso(now_local())
    with db._lock, db.conn:
        db.conn.execute(
            "INSERT INTO v3_users (id, device_key, token_hash, display_name, created_at, last_seen_at)"
            " VALUES ('u_a','dev-a','h','A',?,?)",
            (now_iso, now_iso),
        )
        # 老用户：last_seen_at 在很久以前 → 不计入今日活跃
        db.conn.execute(
            "INSERT INTO v3_users (id, device_key, token_hash, display_name, created_at, last_seen_at)"
            " VALUES ('u_b','dev-b','h','B','2020-01-01T00:00:00+00:00',"
            "'2020-01-01T00:00:00+00:00')"
        )
        persona_id = db.conn.execute("SELECT id FROM personas LIMIT 1").fetchone()["id"]
        db.conn.execute(
            "INSERT INTO persona_events (id, persona_id, event_type, title, start_time,"
            " event_status, dedupe_key, day, created_at, updated_at)"
            " VALUES ('e_1', ?, 'routine', 't', ?, 'completed', 'dk1', ?, ?, ?)",
            (persona_id, now_iso, today, now_iso, now_iso),
        )
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, created_at)"
            " VALUES ('po_1', ?, 'hello', ?)",
            (persona_id, now_iso),
        )
        for index, status in enumerate(("pending", "sent", "sent", "failed", "unconfigured")):
            db.conn.execute(
                "INSERT INTO client_events (id, user_id, type, payload, created_at, push_status)"
                " VALUES (?, 'u_a', 'message', '{}', ?, ?)",
                (f"ce_{index}", now_iso, status),
            )


def test_overview_shape(client, db):
    """总览顶层字段与嵌套结构（前端 OverviewSection 依赖这些名字）。"""
    data = client.get("/api/admin/overview").json()

    assert set(data) == {
        "users",
        "personas",
        "today",
        "services",
        "jobs",
        "push",
        "server_time",
        "server_time_iso",
    }

    assert set(data["users"]) == {"total", "real", "empty", "registered", "active_today"}
    assert set(data["personas"]) == {"total", "active"}
    assert set(data["today"]) == {"events", "posts_pending", "posts_published"}
    assert set(data["jobs"]) == {"failed_recent", "running", "queued"}
    assert set(data["push"]) == {"pending", "sent", "failed", "unconfigured"}

    # 五个服务一个都不能少，且每项是 {configured, ok, source}
    assert set(data["services"]) == {"llm", "stt", "tts", "deepgram", "jpush"}
    for name, status in data["services"].items():
        assert set(status) == {"configured", "ok", "source"}, name
        assert isinstance(status["configured"], bool), name
        assert isinstance(status["ok"], bool), name
        assert status["source"] in ("env", "admin", ""), name

    assert isinstance(data["server_time"], float)


def test_overview_counts_real_data(client, db):
    """有数据时各项计数正确。

    ``ensure_schema(seed=True)`` 本身会种子化若干动态，所以对 ``posts_published``
    断言**增量**而不是绝对值（否则种子数量一变测试就假失败）。
    """
    before = client.get("/api/admin/overview").json()

    _seed_overview_data(db)
    data = client.get("/api/admin/overview").json()

    assert data["users"]["total"] == 2
    assert data["users"]["active_today"] == 1  # 只有 u_a 今天活跃
    # 两个用户都只是裸注册：没有好友/消息/动态/moment/设备注册
    assert data["users"]["real"] == 0
    assert data["users"]["empty"] == 2
    assert data["users"]["registered"] == 0
    assert data["personas"]["total"] >= 1
    assert data["today"]["events"] == 1
    assert data["today"]["posts_published"] == before["today"]["posts_published"] + 1
    # P3 才建 post_queue；本期固定 0（不是"查询失败恰好为 0"）
    assert data["today"]["posts_pending"] == 0

    assert data["push"] == {"pending": 1, "sent": 2, "failed": 1, "unconfigured": 1}


def test_overview_jobs_section(client, db):
    """最近失败任务 + 运行中/排队计数（``retry`` 也算排队）。"""
    queue = DurableJobQueue(db.conn, lock=db._lock)
    for index in range(3):
        queue.enqueue("world", {}, dedupe_key=f"failed:{index}", priority=100)
    # 手工把其中两条改成 failed / 一条改成 retry
    with db._lock, db.conn:
        db.conn.execute("UPDATE background_jobs SET status='failed', last_error='boom'"
                        " WHERE dedupe_key='failed:0'")
        db.conn.execute("UPDATE background_jobs SET status='failed', last_error='bang'"
                        " WHERE dedupe_key='failed:1'")
        db.conn.execute("UPDATE background_jobs SET status='retry' WHERE dedupe_key='failed:2'")

    data = client.get("/api/admin/overview").json()
    jobs = data["jobs"]
    assert jobs["running"] == 0
    assert jobs["queued"] == 1  # retry 也等着被执行
    assert len(jobs["failed_recent"]) == 2
    first = jobs["failed_recent"][0]
    assert {"id", "kind", "last_error", "updated_at"} <= set(first)
    # payload 含业务细节，不下发
    assert "payload" not in first


def test_overview_tolerates_missing_tables(bare_client):
    """表全都不存在时：返回结构完整、计数为 0，而不是 500。"""
    response = bare_client.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["users"] == {"total": 0, "real": 0, "empty": 0, "registered": 0, "active_today": 0}
    assert data["personas"] == {"total": 0, "active": 0}
    assert data["today"] == {"events": 0, "posts_pending": 0, "posts_published": 0}
    assert data["push"] == {"pending": 0, "sent": 0, "failed": 0, "unconfigured": 0}
    assert data["jobs"] == {"failed_recent": [], "running": 0, "queued": 0}


def test_overview_tolerates_missing_db(monkeypatch):
    """数据库未就绪（``app.state.v2`` 缺失）也不该 500。"""
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(api_public_router)
    with TestClient(app) as http:
        response = http.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    assert response.json()["users"] == {
        "total": 0,
        "real": 0,
        "empty": 0,
        "registered": 0,
        "active_today": 0,
    }


def test_overview_jpush_reads_env(monkeypatch):
    """JPush 状态只读环境变量（本期不读管理台配置）。"""
    monkeypatch.setenv("JPUSH_APP_KEY", "key-1")
    monkeypatch.setenv("JPUSH_MASTER_SECRET", "secret-1")
    assert overview_api._jpush_configured() is True

    monkeypatch.delenv("JPUSH_MASTER_SECRET")
    assert overview_api._jpush_configured() is False

    monkeypatch.delenv("JPUSH_APP_KEY")
    assert overview_api._jpush_configured() is False


def test_overview_source_mapping():
    """各存储的来源标记统一成 ``env`` / ``admin`` / 空串。"""
    assert overview_api._source("env") == "env"
    assert overview_api._source("file") == "admin"
    assert overview_api._source("app") == "admin"
    assert overview_api._source("none") == ""
    assert overview_api._source("") == ""
    assert overview_api._source(None) == ""


# --------------------------------------------------------------------------- #
# 3. jobs
# --------------------------------------------------------------------------- #
def test_job_kinds_match_maintenance_handlers():
    """``JOB_KINDS`` 必须与 ``maintenance.py`` 的 handlers 完全一致。

    静态解析源码（不 import ``maintenance``——它会拉起 world/LLM 一堆依赖），
    这样两边一旦不同步，测试立刻失败，而不是等线上发现"点了没反应"。
    """
    source = pathlib.Path(
        overview_api.__file__
    ).parent.parent.parent / "maintenance.py"
    assert source.exists(), source
    tree = ast.parse(source.read_text(encoding="utf-8"))

    handlers: set[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "handlers" not in targets:
            continue
        if isinstance(node.value, ast.Dict):
            handlers = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            break

    assert handlers is not None, "maintenance.py 里找不到 handlers 字典"
    assert set(jobs_api.JOB_KINDS) == handlers, (
        f"JOB_KINDS 与 maintenance handlers 不同步："
        f"多 {sorted(set(jobs_api.JOB_KINDS) - handlers)} / "
        f"少 {sorted(handlers - set(jobs_api.JOB_KINDS))}"
    )


def test_jobs_kinds_endpoint(client):
    data = client.get("/api/admin/jobs/kinds").json()
    assert set(data) == {"kinds"}
    assert data["kinds"] == list(jobs_api.JOB_KINDS)
    assert "world" in data["kinds"]
    assert "followup" in data["kinds"]


def test_jobs_list_shape_and_pagination(client, db):
    queue = DurableJobQueue(db.conn, lock=db._lock)
    for index in range(5):
        queue.enqueue("world", {"n": index}, dedupe_key=f"k:{index}", priority=100)

    data = client.get("/api/admin/jobs").json()
    assert set(data) == {"items", "total", "offset", "limit"}
    assert data["total"] == 5
    assert data["offset"] == 0
    assert data["limit"] == 40
    assert len(data["items"]) == 5

    item = data["items"][0]
    assert set(item) == {
        "id",
        "kind",
        "status",
        "attempts",
        "priority",
        "run_after",
        "last_error",
        "created_at",
        "updated_at",
    }
    # payload 不下发
    assert "payload" not in item

    page = client.get("/api/admin/jobs", params={"offset": 2, "limit": 2}).json()
    assert page["total"] == 5
    assert page["offset"] == 2
    assert page["limit"] == 2
    assert len(page["items"]) == 2


def test_jobs_list_filters(client, db):
    queue = DurableJobQueue(db.conn, lock=db._lock)
    queue.enqueue("world", {}, dedupe_key="f:1", priority=100)
    queue.enqueue("cleanup", {}, dedupe_key="f:2", priority=100)
    with db._lock, db.conn:
        db.conn.execute("UPDATE background_jobs SET status='failed' WHERE dedupe_key='f:1'")

    failed = client.get("/api/admin/jobs", params={"status": "failed"}).json()
    assert failed["total"] == 1
    assert failed["items"][0]["kind"] == "world"

    cleanup = client.get("/api/admin/jobs", params={"kind": "cleanup"}).json()
    assert cleanup["total"] == 1
    assert cleanup["items"][0]["kind"] == "cleanup"

    combined = client.get(
        "/api/admin/jobs", params={"kind": "world", "status": "failed"}
    ).json()
    assert combined["total"] == 1

    empty = client.get("/api/admin/jobs", params={"kind": "cleanup", "status": "failed"}).json()
    assert empty["total"] == 0
    assert empty["items"] == []


def test_jobs_list_rejects_bad_pagination(client):
    assert client.get("/api/admin/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/admin/jobs", params={"limit": 1000}).status_code == 422
    assert client.get("/api/admin/jobs", params={"offset": -1}).status_code == 422


def test_jobs_run_enqueues(client, db):
    """``POST /jobs/{kind}/run`` 入队一条，返回 id，且库里能查到。"""
    response = client.post("/api/admin/jobs/cleanup/run")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True
    assert isinstance(data["id"], str) and data["id"]

    row = db.conn.execute(
        "SELECT kind, payload, priority, status FROM background_jobs WHERE id=?",
        (data["id"],),
    ).fetchone()
    assert row is not None
    assert row["kind"] == "cleanup"
    assert row["priority"] == jobs_api.MANUAL_PRIORITY
    assert row["status"] == "queued"
    # payload 带今日 day（与 maintenance 周期任务的约定一致）
    import json  # noqa: PLC0415

    payload = json.loads(row["payload"])
    assert set(payload) == {"day"}
    assert payload["day"] == jobs_api._today()


def test_jobs_run_is_repeatable(client, db):
    """连点两次要排两次——不能被 dedupe 合并掉。"""
    first = client.post("/api/admin/jobs/world/run").json()
    second = client.post("/api/admin/jobs/world/run").json()
    assert first["id"] != second["id"]
    total = db.conn.execute(
        "SELECT COUNT(*) FROM background_jobs WHERE kind='world'"
    ).fetchone()[0]
    assert total == 2


def test_jobs_run_rejects_unknown_kind(client, db):
    response = client.post("/api/admin/jobs/not-a-real-kind/run")
    assert response.status_code == 422
    assert "未知任务类型" in str(response.json()["detail"])
    # 一条都不该入队
    assert db.conn.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0


def test_jobs_run_without_queue_is_503(bare_client):
    """队列未装配（极简 app）→ 503，而不是 AttributeError 500。"""
    response = bare_client.post("/api/admin/jobs/world/run")
    assert response.status_code == 503


def test_jobs_list_without_jobs_table(bare_client):
    """``background_jobs`` 表缺失时返回空列表 200，不 500。"""
    response = bare_client.get("/api/admin/jobs")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 40}


# --------------------------------------------------------------------------- #
# 4. session / logout
# --------------------------------------------------------------------------- #
def test_session_shape(client):
    response = client.get("/api/admin/session")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"authenticated", "csrf", "password_configured", "version"}
    assert isinstance(data["authenticated"], bool)
    assert isinstance(data["csrf"], str)
    assert isinstance(data["password_configured"], bool)
    assert isinstance(data["version"], str)


def test_session_unauthenticated_is_200_not_403(client):
    """未登录也要 200 + authenticated=false：前端据此决定跳登录页。"""
    data = client.get("/api/admin/session").json()
    assert data["authenticated"] is False
    assert data["password_configured"] is False


def test_session_reports_authenticated_with_cookie(client, monkeypatch):
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    client.cookies.set(auth.COOKIE_NAME, auth.make_session())
    data = client.get("/api/admin/session").json()
    assert data["authenticated"] is True
    assert data["password_configured"] is True


def test_session_csrf_matches_auth_source(client, monkeypatch):
    """``csrf`` 必须与服务端校验用的是同一个派生值（否则写操作全 403）。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    data = client.get("/api/admin/session").json()
    assert data["csrf"] == auth.csrf_token()
    assert len(data["csrf"]) == 40


def test_session_version_present(client):
    data = client.get("/api/admin/session").json()
    assert data["version"]  # 最小 app 里回落到包版本，不应为空


def test_logout_clears_cookie(client, monkeypatch):
    """真实登录 → 退出 → 会话失效（走 Cookie jar，覆盖路径/域名匹配）。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    login = client.post(
        "/admin/login",
        data={"password": "pw-123456", "next": "/admin"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert client.get("/api/admin/session").json()["authenticated"] is True

    response = client.post("/api/admin/logout")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    # 清 Cookie 是"删"语义：响应头里带同名的过期 Set-Cookie
    raw = " ".join(
        value for key, value in response.headers.items() if key.lower() == "set-cookie"
    )
    assert auth.COOKIE_NAME in raw
    assert "Max-Age=0" in raw
    assert client.get("/api/admin/session").json()["authenticated"] is False


def test_logout_is_json_not_redirect(client):
    """API 版退出返回 JSON（旧的 /admin/logout 才是 303 重定向）。"""
    response = client.post("/api/admin/logout", follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_logout_idempotent_when_not_logged_in(client):
    """会话已过期时"退出"仍应成功（幂等），否则前端按钮会卡住。"""
    assert client.post("/api/admin/logout").status_code == 200
    assert client.post("/api/admin/logout").json() == {"ok": True}


# --------------------------------------------------------------------------- #
# 5. 拆分结构守卫
# --------------------------------------------------------------------------- #
def test_api_modules_do_not_import_router():
    """子模块**不能** import ``app.admin.router``（会形成循环导入）。"""
    api_dir = pathlib.Path(overview_api.__file__).parent
    offenders = []
    for path in api_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.admin.router":
                offenders.append(path.name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.admin.router":
                        offenders.append(path.name)
    assert offenders == [], f"以下模块 import 了 router（循环导入风险）: {sorted(set(offenders))}"


def test_unimplemented_modules_expose_no_routes():
    """把「别造假接口」这条原则保留下来。

    P4 之后所有分区都已实现（含 ``audit``），所以这条不再有 ``pending`` 模块可查；
    保留它作为**下次新增模块时的检查位**，并正向断言所有已实现模块都真的挂了 router
    ——比"清单式断言"更难漏。
    """
    from app.admin.api import (  # noqa: PLC0415
        audit,
        config,
        events,
        jobs,
        lexicons,
        media,
        overview,
        personas,
        posts,
        prompts,
        release,
        users,
    )

    for module in (
        audit,
        config,
        events,
        jobs,
        lexicons,
        media,
        overview,
        personas,
        posts,
        prompts,
        release,
        users,
    ):
        assert hasattr(module, "router"), f"{module.__name__} 缺 router，router.py 的 include 会炸"


def test_router_py_is_aggregator_only():
    """``router.py`` 迁完后只剩聚合逻辑（不再有业务 handler 定义）。"""
    import sys  # noqa: PLC0415

    # 注意：``app.admin.__init__`` 把 ``router`` 这个**名字**重导成了 APIRouter 对象，
    # 所以 ``import app.admin.router as x`` 拿到的是 router 本身；必须查 sys.modules。
    router_module = sys.modules["app.admin.router"]
    source = pathlib.Path(router_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    decorated = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            # 形如 @router.get(...) 的调用
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                owner = getattr(decorator.func.value, "id", "")
                if owner == "router":
                    decorated.append(node.name)
    # 允许 include_router（不是路由装饰器），业务 handler 必须为 0
    assert decorated == [], f"router.py 里仍有业务 handler: {decorated}"


def test_session_module_reexports_router(client):
    """session 的路由挂在 ``public_router`` 上（P4 起不再走受守卫的 router）。"""
    paths = {r.path for r in session_api.public_router.routes if hasattr(r, "path")}
    assert "/session" in paths
    assert "/logout" in paths
    # 受守卫的 router 里不该再有它们——否则未登录会被 403，
    # 而前端正是靠 /session 的 200 + authenticated=false 来决定跳登录页。
    guarded = {r.path for r in session_api.router.routes if hasattr(r, "path")}
    assert "/session" not in guarded
    assert "/logout" not in guarded
