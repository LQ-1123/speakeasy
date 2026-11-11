"""P2 验收：构建产物接线 + 各分区 API 契约实测。

为什么不用浏览器点
------------------
本机 `agent-browser` 的 `open` 会偶发/稳定挂死（profile 锁 + daemon 卡住），
不适合放进可重复的验收脚本。但"分区有没有真的接上、API 形状对不对"这两件事
可以用**确定性**的方式证明：

1. **构建产物**：每个分区都有一段独有的中文文案（压缩后字符串仍保留）。
   在 `backend/app/static/app/_next/**/*.js` 里搜到它，说明该分区确实被编译进
   了管理台 bundle——typo 的 dynamic import 会在这里暴露。
2. **API 契约**：用真 Cookie 会话逐个打每个分区依赖的接口，校验关键字段存在。
   页面崩掉最常见的原因就是"后端返回的形状和前端假设不一致"。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx

BASE = os.environ.get("SE_E2E_BASE", "http://127.0.0.1:8899")
PASSWORD = os.environ.get("SE_ADMIN_PASSWORD", "test-pw-123456")
ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "backend" / "app" / "static" / "app"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# --------------------------------------------------------------------------- #
# 1. 构建产物：每个分区独有的文案
# --------------------------------------------------------------------------- #

#: 分区 → (说明, 必须出现在产物里的独有字符串)
SECTION_MARKERS: dict[str, tuple[str, list[str]]] = {
    "DnaSection": ("表达 DNA", ["设为默认", "恢复默认"]),
    "ScenesSection": ("场景与口头禅", ["口头禅", "禁用词"]),
    "RhythmSection": ("世界节奏", ["模拟今天", "分享权重"]),
    "PoolSection": ("捏人素材池", ["预览捏人", "素材池"]),
    "StudioSection": ("角色工坊", ["彻底重建", "重新分配音色"]),
    "VoicesSection": ("音色池", ["试听", "音色池"]),
    "ConfigSection": ("API 与密钥", ["测试连接", "先验证再落盘"]),
    "ReleaseSection": ("版本发布", ["回滚到上一版", "强制更新"]),
    "MediaSection": ("图片素材", ["搜图入库", "清理图库"]),
}


def verify_bundle() -> None:
    js_files = sorted(STATIC.glob("_next/**/*.js"))
    check(bool(js_files), "管理台 bundle 存在", f"{len(js_files)} 个 js")
    blob = ""
    for path in js_files:
        try:
            blob += path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    check(len(blob) > 100_000, "bundle 有实质内容", f"{len(blob)//1024} KB")

    for name, (label, markers) in SECTION_MARKERS.items():
        missing = [m for m in markers if m not in blob]
        check(not missing, f"{name}（{label}）已编译进 bundle", f"缺少 {missing}" if missing else "")


# --------------------------------------------------------------------------- #
# 2. API 契约
# --------------------------------------------------------------------------- #


def verify_apis(client: httpx.Client) -> None:
    def get(path: str) -> tuple[int, object]:
        resp = client.get(path)
        try:
            return resp.status_code, resp.json()
        except Exception:  # noqa: BLE001
            return resp.status_code, None

    def need_keys(ok: bool, name: str, payload: object, keys: list[str]) -> None:
        if not ok or not isinstance(payload, dict):
            check(False, name, "响应不是 JSON 对象")
            return
        missing = [k for k in keys if k not in payload]
        check(not missing, name, f"缺少字段 {missing}" if missing else "")

    # DNA：fields + usage
    code, data = get("/api/admin/lexicons/dna")
    ok = code == 200 and isinstance(data, dict) and isinstance(data.get("data"), dict)
    need_keys(ok, "GET /lexicons/dna", data.get("data") if isinstance(data, dict) else None, ["fields"])

    code, usage = get("/api/admin/usage")
    need_keys(code == 200, "GET /usage", usage, ["personas", "dna"])

    # scenes / phrases
    code, data = get("/api/admin/lexicons/scenes")
    need_keys(code == 200, "GET /lexicons/scenes", data.get("data") if isinstance(data, dict) else None, ["scenes"])
    code, data = get("/api/admin/lexicons/phrases")
    need_keys(
        code == 200,
        "GET /lexicons/phrases",
        data.get("data") if isinstance(data, dict) else None,
        ["banned_global", "signature_pools"],
    )

    # rhythm / events / world
    code, data = get("/api/admin/lexicons/rhythm")
    params = (data.get("data") or {}).get("params") if isinstance(data, dict) else None
    need_keys(
        code == 200,
        "GET /lexicons/rhythm",
        params,
        [
            "life_tick_s",
            "posts_tick_s",
            "posts_max_events",
            "posts_since_hours",
            "event_posts_per_day",
            "tension_penalty_step",
            "tension_penalty_floor",
            "comment_backfill_hours",
        ],
    )
    code, data = get("/api/admin/lexicons/events")
    need_keys(code == 200, "GET /lexicons/events", data.get("data") if isinstance(data, dict) else None, ["types"])
    code, data = get("/api/admin/lexicons/world")
    world = data.get("data") if isinstance(data, dict) else None
    need_keys(code == 200, "GET /lexicons/world", world, ["names", "occupations", "cities", "interests"])

    # 模拟今天
    code, data = get("/api/admin/simulate")
    need_keys(code == 200, "GET /simulate", data, ["params", "types", "world", "today"])

    # 预览捏人
    code, data = get("/api/admin/preview-personas?count=2&language=en")
    check(code == 200 and isinstance(data, dict) and "personas" in data, "GET /preview-personas", f"HTTP {code}")

    # 角色工坊
    code, data = get("/api/admin/personas?limit=3")
    need_keys(code == 200, "GET /personas", data, ["ok", "personas"])

    # 音色池
    code, data = get("/api/admin/voice-pool")
    check(code == 200 and isinstance(data, dict) and data.get("ok") is not False, "GET /voice-pool", f"HTTP {code}")

    # 图片库
    code, data = get("/api/admin/media/stats")
    need_keys(code == 200, "GET /media/stats", data, ["ok", "files", "bytes_mb", "referenced_files"])
    code, data = get("/api/admin/media/library?offset=0&limit=5")
    need_keys(code == 200, "GET /media/library", data, ["ok", "items", "total"])

    # 配置 / 发布 / 任务
    code, data = get("/api/admin/config")
    need_keys(code == 200, "GET /config", data, ["llm", "stt", "tts", "deepgram"])
    code, data = get("/api/admin/app-release")
    need_keys(code == 200, "GET /app-release", data, ["current", "history", "files"])
    code, data = get("/api/admin/jobs?limit=1")
    need_keys(code == 200, "GET /jobs", data, ["total", "items"])


def main() -> int:
    verify_bundle()

    with httpx.Client(base_url=BASE, follow_redirects=False, trust_env=False, timeout=30.0) as client:
        resp = client.post("/admin/login", data={"password": PASSWORD, "next": "/admin"})
        logged_in = resp.status_code in (303, 302, 307)
        check(logged_in, "管理台登录", f"HTTP {resp.status_code}")

        if logged_in:
            verify_apis(client)

    failed = [r for r in results if not r[0]]
    print(f"\n合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for _ok, name, detail in failed:
            print(f"  - {name}  {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
