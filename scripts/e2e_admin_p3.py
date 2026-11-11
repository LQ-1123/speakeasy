"""P3 端到端：对真实 uvicorn 验证新增的管理能力。

为什么要有这个脚本
------------------
单元测试跑在 TestClient + 临时库上，证明不了"真服务上这条路走得通"。
P3 引入的都是**有副作用**的能力（发好友申请、领事件、发动态、改 prompt），
一旦在真实环境里写坏数据，回滚很麻烦——所以在隔离 DATA_DIR 上先跑一遍。

覆盖：
- 构建产物里 5 个 P3 分区的独有文案（接线错会在这里暴露）
- 用户管理：列表 / 详情（确认不含聊天记录）/ 候选角色
- 好友指派：直接加好友 + **幂等**
- 事件公共池：建池 / 预演（分项贡献之和=总分）/ 领取 / **幂等**
- 动态：写一条入队 / 部分更新 / 软取消 / 立即发布 / **幂等**
- Prompt：读模块（必须带原文）/ 保存改写 / **改写真的进 compose_prompt** / 清空 / 模板
- JPush：config 快照带脱敏状态
- 图片上传：真发一张 PNG，确认重命名落库
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys

import httpx

BASE = os.environ.get("SE_E2E_BASE", "http://127.0.0.1:8899")
PASSWORD = os.environ.get("SE_ADMIN_PASSWORD", "test-pw-123456")
#: 播种用户要写到服务用的那个库；与服务端 DATA_DIR 必须一致。
DATA_DIR = os.environ.get("SE_E2E_DATA_DIR", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "backend" / "app" / "static" / "app"

results: list[tuple[bool, str, str]] = []
skipped: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def skip(name: str, reason: str) -> None:
    """环境不具备（不是被测代码的问题），记为跳过而不是失败。"""
    skipped.append(f"{name}（{reason}）")
    print(f"SKIP  {name}  {reason}")


def seed_user(data_dir: str) -> str:
    """在服务库里播一个用户 + 一台设备，供「用户管理」用例使用。

    为什么直接写库：正常用户是通过 App 首次启动的 device_key→token 流程产生的，
    在 e2e 里复现那套握手很啰嗦，且会牵出与本期无关的鉴权逻辑。
    写库属于测试 fixture 的准备步骤，不改变被测行为。
    """
    import sqlite3

    db_path = str(pathlib.Path(data_dir) / "speakeasy_v2.db")
    if not pathlib.Path(db_path).is_file():
        return ""
    conn = sqlite3.connect(db_path)
    try:
        uid = "u_e2e_fixture"
        conn.execute(
            "INSERT OR IGNORE INTO v3_users (id, device_key, token_hash, display_name,"
            " created_at, last_seen_at) VALUES (?,?,?,?,?,?)",
            (uid, "dev_e2e_fixture_key", "", "E2E 测试用户", "2026-11-11T00:00:00", "2026-11-11T00:00:00"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO client_devices"
            " (registration_id, user_id, platform, vendor, enabled, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("e2e_registration_id", uid, "android", "e2e", 1, 1.0),
        )
        conn.commit()
        return uid
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. 构建产物：P3 分区必须真的编译进 bundle
# --------------------------------------------------------------------------- #
SECTION_MARKERS: dict[str, list[str]] = {
    "UsersSection": ["设备尾号"],
    "UserDetailSection": ["直接成为好友", "管理台不提供聊天记录"],
    "PostsSection": ["取消（保留记录）", "存为待发"],
    "EventsSection": ["预演", "已领满"],
    "PromptSection": ["只读", "保存后立即生效"],
}


def verify_bundle() -> None:
    blob = ""
    for path in sorted(STATIC.glob("_next/**/*.js")):
        try:
            blob += path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    check(len(blob) > 200_000, "bundle 有实质内容", f"{len(blob)//1024} KB")
    for name, markers in SECTION_MARKERS.items():
        missing = [m for m in markers if m not in blob]
        check(not missing, f"{name} 已编译进 bundle", f"缺少 {missing}" if missing else "")


# --------------------------------------------------------------------------- #
def main() -> int:
    verify_bundle()

    # 用户管理需要有用户可查；没有就播一个（见 seed_user 的说明）。
    if DATA_DIR:
        seeded = seed_user(DATA_DIR)
        check(bool(seeded), "播种测试用户", seeded or "库不存在")

    with httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False, timeout=60.0) as c:
        resp = c.post("/admin/login", data={"password": PASSWORD, "next": "/admin"})
        check(resp.status_code in (303, 302), "管理台登录", f"HTTP {resp.status_code}")
        if resp.status_code not in (303, 302):
            return 1

        # P4 起 CSRF 全强制：Cookie 身份的写请求必须带 X-Admin-CSRF。
        # 设成 client 默认头，下面所有写请求自动带上（GET 带它无害）。
        csrf = str(c.get("/api/admin/session").json().get("csrf") or "")
        check(bool(csrf), "拿到 CSRF 令牌", csrf[:8] + "…")
        c.headers["X-Admin-CSRF"] = csrf

        def get(path: str) -> dict:
            r = c.get(path)
            try:
                return r.json()
            except Exception:  # noqa: BLE001
                return {"ok": False, "issues": [f"HTTP {r.status_code}"]}

        def post(path: str, payload: dict | None = None) -> dict:
            r = c.post(path, json=payload or {})
            try:
                return r.json()
            except Exception:  # noqa: BLE001
                return {"ok": False, "issues": [f"HTTP {r.status_code}"]}

        # ---------------- 角色（后续用例要用真实角色 id） ----------------
        personas = get("/api/admin/personas?limit=5")
        items = personas.get("personas") or personas.get("items") or []
        check(bool(items), "能取到角色列表", f"{len(items)} 个")
        if not items:
            print("没有角色，后续用例无法进行")
            return 1
        pid = str(items[0].get("id") or "")
        pid2 = str(items[1].get("id") or pid) if len(items) > 1 else pid

        # ---------------- 用户管理 ----------------
        users = get("/api/admin/users?limit=5")
        check(users.get("ok") is True, "GET /users", str(users.get("issues") or ""))
        check("items" in users and "total" in users, "用户列表形状", f"total={users.get('total')}")
        # device_key 只回尾号（不能泄露完整设备号）
        leaked = [
            row for row in (users.get("items") or [])
            if any(k in row for k in ("device_key", "token_hash"))
        ]
        check(not leaked, "用户列表不泄露完整 device_key/token", f"{len(leaked)} 行违规")

        uid = ""
        if users.get("items"):
            uid = str(users["items"][0]["id"])
            detail = get(f"/api/admin/users/{uid}")
            check(detail.get("ok") is True, "GET /users/{uid}", str(detail.get("issues") or ""))
            check("messages" not in json.dumps(detail, ensure_ascii=False), "用户详情不含聊天记录")
            check(isinstance(detail.get("devices"), list), "详情含 devices")
            check(isinstance(detail.get("friends"), list), "详情含 friends")
            cands = get(f"/api/admin/users/{uid}/candidate-personas?limit=5")
            check(cands.get("ok") is True, "GET candidate-personas")
        else:
            skip("用户详情/好友指派", "库里没有用户")

        # ---------------- 好友指派 + 幂等 ----------------
        if uid:
            first = post(f"/api/admin/users/{uid}/friends", {"persona_id": pid2, "mode": "direct"})
            check(first.get("ok") is True, "直接加好友", str(first.get("issues") or ""))
            again = post(f"/api/admin/users/{uid}/friends", {"persona_id": pid2, "mode": "direct"})
            check(again.get("ok") is False, "重复加好友被拒绝（幂等）", str(again.get("issues") or ""))
            # 清场，避免影响后面的候选列表断言
            fid = (first.get("friendship") or {}).get("id")
            if fid:
                removed = c.delete(f"/api/admin/users/{uid}/friends/{fid}").json()
                check(removed.get("ok") is True, "解除好友", str(removed.get("issues") or ""))

        # ---------------- 事件公共池 ----------------
        created = post(
            "/api/admin/event-pool",
            {
                "title": "E2E 周末市集",
                "description": "在河边的手工市集",
                "event_type": "social",
                "location": "上海",
                "slots": 2,
            },
        )
        check(created.get("ok") is True, "创建事件池", str(created.get("issues") or ""))
        pool_id = str((created.get("item") or {}).get("id") or created.get("id") or "")

        if pool_id:
            preview = post(f"/api/admin/event-pool/{pool_id}/preview", {"limit": 8})
            check(preview.get("ok") is True, "预演领取", str(preview.get("issues") or ""))
            pitems = preview.get("items") or []
            check(bool(pitems), "预演有候选角色", f"{len(pitems)} 个")
            # 分项贡献之和应等于总分（可分项解释排名）
            if pitems:
                first_row = pitems[0]
                detail_map = first_row.get("score_detail") or {}
                con = detail_map.get("contributions") if isinstance(detail_map.get("contributions"), dict) else None
                if con is None:
                    con = {
                        k: v.get("contribution")
                        for k, v in detail_map.items()
                        if isinstance(v, dict) and isinstance(v.get("contribution"), (int, float))
                    }
                total = first_row.get("score")
                if con and isinstance(total, (int, float)):
                    s = sum(float(v) for v in con.values() if isinstance(v, (int, float)))
                    check(abs(s - float(total)) < 0.51, "分项贡献之和≈总分", f"{s:.1f} vs {total}")
                else:
                    check(False, "分项得分明细存在", f"keys={list(detail_map)[:6]}")

            claim = post(f"/api/admin/event-pool/{pool_id}/claim", {})
            check(claim.get("ok") is True, "按分自动领取", str(claim.get("issues") or ""))
            claimed = len(claim.get("claimed") or [])
            check(claimed > 0, "领取成功", f"{claimed} 个角色")
            again = post(f"/api/admin/event-pool/{pool_id}/claim", {})
            claim_skipped = again.get("skipped") or []
            already = again.get("ok") is False or bool(claim_skipped)
            check(
                already,
                "重复领取被跳过（幂等）",
                f"skipped={len(claim_skipped)} ok={again.get('ok')}",
            )

            deleted = c.delete(f"/api/admin/event-pool/{pool_id}").json()
            check(deleted.get("ok") is True, "删除事件池", str(deleted.get("issues") or ""))

        # ---------------- 动态队列 ----------------
        pending0 = get("/api/admin/posts/pending?limit=1")
        check(pending0.get("ok") is True, "GET /posts/pending")

        made = post(
            "/api/admin/posts",
            {"persona_id": pid, "content": "E2E 手写的一条动态", "publish_now": False},
        )
        check(made.get("ok") is True, "写一条入队", str(made.get("issues") or ""))
        qid = str(made.get("id") or (made.get("item") or {}).get("id") or "")
        if qid:
            edited = c.put(f"/api/admin/posts/{qid}", json={"content": "E2E 改过的内容"}).json()
            check(edited.get("ok") is True, "编辑待发（部分更新）", str(edited.get("issues") or ""))
            item = edited.get("item") or {}
            check(item.get("content") == "E2E 改过的内容", "内容已更新")

            pub = post(f"/api/admin/posts/{qid}/publish-now", {})
            check(pub.get("ok") is True, "立即发布", str(pub.get("issues") or ""))
            post_id = str(pub.get("post_id") or "")
            again = post(f"/api/admin/posts/{qid}/publish-now", {})
            same = again.get("already") is True or str(again.get("post_id") or "") == post_id
            check(same, "重复发布不产生第二条（幂等）", f"post_id={post_id}")

            # 已发布的不能再编辑
            late = c.put(f"/api/admin/posts/{qid}", json={"content": "不该成功"}).json()
            check(late.get("ok") is False, "已发布不可编辑", str(late.get("issues") or ""))

        # 软取消
        made2 = post("/api/admin/posts", {"persona_id": pid, "content": "E2E 要取消的一条"})
        qid2 = str(made2.get("id") or (made2.get("item") or {}).get("id") or "")
        if qid2:
            cancelled = c.delete(f"/api/admin/posts/{qid2}").json()
            check(cancelled.get("ok") is True, "软取消", str(cancelled.get("issues") or ""))
            again = c.delete(f"/api/admin/posts/{qid2}").json()
            check(again.get("ok") is True and (again.get("already") is True or again.get("cancelled")),
                  "重复取消幂等", str(again))

        # ---------------- Prompt 控制台 ----------------
        mods = get(f"/api/admin/prompt/modules?persona_id={pid}&mode=text")
        check(mods.get("ok") is True, "GET /prompt/modules", str(mods.get("issues") or ""))
        mlist = mods.get("modules") or []
        check(bool(mlist), "模块清单非空", f"{len(mlist)} 个")
        # 只读模块也必须带原文（否则界面没法展示"为什么不能改"）
        readonly = [m for m in mlist if m.get("editable") is False]
        check(bool(readonly), "存在只读模块", f"{len(readonly)} 个")
        check(all("text" in m for m in mlist), "每个模块都带 text 原文")
        check(any((m.get("text") or "").strip() for m in readonly), "只读模块的原文非空")

        marker = "E2E：这个角色说话前会先停顿一下。"
        saved = c.put(
            "/api/admin/prompt/overrides",
            json={"persona_id": pid, "mode": "text", "modules": {"character_card": marker}},
        ).json()
        check(saved.get("ok") is True, "保存模块改写", str(saved.get("issues") or ""))

        after = get(f"/api/admin/prompt/modules?persona_id={pid}&mode=text")
        after_card = next(
            (m.get("text") for m in (after.get("modules") or []) if m.get("key") == "character_card"),
            "",
        )
        check(after_card == marker, "改写已生效（modules 回读一致）", after_card[:40])

        preview2 = post(
            "/api/admin/prompt/preview", {"persona_id": pid, "mode": "text", "text": "在吗"}
        )
        full = str(preview2.get("full_text") or "")
        check(marker in full, "改写进入真实组装 prompt", f"full_text {len(full)} 字")
        check("trimmed" in preview2 and "max_chars" in preview2, "预览含预算信息")

        cleared = c.delete(f"/api/admin/prompt/overrides?persona_id={pid}&mode=text").json()
        check(cleared.get("ok") is True, "清空改写", str(cleared.get("issues") or ""))
        after2 = get(f"/api/admin/prompt/modules?persona_id={pid}&mode=text")
        card2 = next(
            (m.get("text") for m in (after2.get("modules") or []) if m.get("key") == "character_card"),
            "",
        )
        check(card2 != marker, "清空后回到原文")

        tpl = post("/api/admin/prompt/templates", {"name": "E2E 模板", "modules": {"character_card": "冷淡"}})
        check(tpl.get("ok") is True, "建模板", str(tpl.get("issues") or ""))
        tpl_id = str(tpl.get("id") or "")
        tlist = get("/api/admin/prompt/templates")
        check(any(t.get("name") == "E2E 模板" for t in (tlist.get("templates") or [])), "模板出现在列表")
        if tpl_id:
            dt = c.delete(f"/api/admin/prompt/templates/{tpl_id}").json()
            check(dt.get("ok") is True, "删模板", str(dt.get("issues") or ""))

        # ---------------- JPush 状态 ----------------
        cfg = get("/api/admin/config")
        jpush = cfg.get("jpush") or {}
        check("configured" in jpush, "config 快照含 jpush", str(jpush)[:80])
        # 脱敏校验：不许出现**密钥字段**（值是明文那种），只允许布尔与尾号。
        # 之前我用 "master_secret" 子串匹配，被 `has_master_secret`（布尔）误判——
        # 键名不等于泄露，要看它是不是承载了明文。
        plaintext_fields = [
            key
            for key, value in jpush.items()
            if key in ("app_key", "master_secret", "app_secret") and isinstance(value, str) and value
        ]
        tail = str(jpush.get("app_key_tail") or "")
        check(
            not plaintext_fields and len(tail) <= 8,
            "jpush 状态已脱敏（无明文密钥）",
            f"明文字段={plaintext_fields} tail={tail!r}",
        )

        # ---------------- 图片上传 ----------------
        png = _tiny_png()
        up = c.post(
            "/api/admin/media/upload",
            files={"file": ("evil/../../名字.png", io.BytesIO(png), "image/png")},
        )
        try:
            upj = up.json()
        except Exception:  # noqa: BLE001
            upj = {"ok": False, "issues": [f"HTTP {up.status_code}"]}
        check(upj.get("ok") is True, "上传图片", str(upj.get("issues") or ""))
        path = str(upj.get("path") or "")
        check(bool(path) and ".." not in path and "evil" not in path, "上传文件被重命名（防穿越）", path)

        # ---------------- 总览的待发数已接真实队列 ----------------
        ov = get("/api/admin/overview")
        pending_count = (ov.get("today") or {}).get("posts_pending")
        check(isinstance(pending_count, int), "overview.today.posts_pending 是整数", str(pending_count))

    failed = [r for r in results if not r[0]]
    print(f"\n合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}，跳过 {len(skipped)}")
    if skipped:
        print("跳过项（环境不具备，不是被测代码的问题）：")
        for item in skipped:
            print(f"  - {item}")
    if failed:
        print("失败项：")
        for _ok, name, detail in failed:
            print(f"  - {name}  {detail}")
    return 0 if not failed else 1


def _tiny_png() -> bytes:
    """造一张真的 PNG。

    一开始我手写了一个 base64 常量，结果被 Pillow 判为无效图片——
    上传端点的校验是对的，是我的测试数据不对。用 Pillow 生成最稳。
    """
    try:
        from PIL import Image  # noqa: PLC0415

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (90, 140, 200)).save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - 没有 Pillow 时退回一个已知有效的 1x1 PNG
        import base64

        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )


if __name__ == "__main__":
    sys.exit(main())
