"""本机 Web 功能验证（Onboarding 建档）：真实 Chrome + CDP，不是 Mock。

因为本机 DSH 作业对象不允许从 Python 直接拉起 Chrome，本脚本**附着**到由
PowerShell 启动的 Chrome（见 ``scripts/run_local_web_verify.ps1``）。

用法（由编排脚本调用）：
    python scripts/verify_local_web.py --scenario main --port 9401
可用 scenario：main / reopen / legacy / mobile / offline / isolation / isolation-check / summary
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp_driver import Chrome  # noqa: E402

BASE = "http://127.0.0.1:8000"
SHOTS = ROOT / "artifacts" / "onboarding-verify"
# 子进程（python）在本机沙箱里只能写 workspace 根 / .tmp / artifacts，
# 因此报告先写 artifacts/qa-reports，收尾时由 PowerShell 复制到 docs/qa 留档。
REPORT = ROOT / "artifacts" / "qa-reports"
TMP = ROOT / ".tmp"
#: 报告文件名前缀（本机 = local-web，远端 = remote-web，由 --prefix 覆盖）
PREFIX = "local-web"
#: 某个 scenario 的 summary 需要包含哪些场景（由 --expected 覆盖）
EXPECTED_DEFAULT = [
    "main",
    "reopen",
    "legacy",
    "mobile",
    "offline",
    "isolation",
    "isolation-check",
    "chat",
]

results: list[dict[str, Any]] = []


def check(name: str, ok: bool, detail: Any = "") -> bool:
    item = {"name": name, "ok": bool(ok), "detail": detail, "at": datetime.now().isoformat(timespec="seconds")}
    results.append(item)
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  -> {detail}"))
    return bool(ok)


def shot(driver: Chrome, name: str) -> str:
    path = SHOTS / f"{name}.png"
    if not path.exists():
        # 品牌开屏是覆盖层：等它淡出再截图，否则证据图全是 SONDER 开屏。
        try:
            driver.wait_js(
                "(() => { const e=document.getElementById('sonder-splash');"
                " return !e || e.classList.contains('is-done')"
                " || getComputedStyle(e).visibility === 'hidden'; })()",
                timeout=10,
            )
        except Exception:  # noqa: BLE001 - 没有开屏时直接截
            pass
        time.sleep(0.35)
        driver.screenshot(str(path))
    return str(path.relative_to(ROOT)).replace("\\", "/")


def in_onboarding(driver: Chrome) -> bool:
    return bool(driver.eval("!!document.querySelector('.se-onb-screen')"))


def app_ready(driver: Chrome) -> bool:
    return bool(driver.eval("!!document.querySelector('.w4-app') && !document.querySelector('.se-onb-screen')"))


def wait_app_or_onboarding(driver: Chrome, timeout: float = 60.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if in_onboarding(driver):
            return "onboarding"
        if app_ready(driver):
            return "app"
        time.sleep(0.3)
    raise RuntimeError("60s 内既没有引导也没有主界面")


def no_horizontal_overflow(driver: Chrome) -> tuple[bool, dict[str, Any]]:
    info = driver.eval(
        "(() => { const de=document.documentElement; return {scroll: de.scrollWidth,"
        " inner: window.innerWidth, body: document.body ? document.body.scrollWidth : 0}; })()"
    )
    return info["scroll"] <= info["inner"] + 1 and info["body"] <= info["inner"] + 1, info


def click_in_group(driver: Chrome, group_aria: str, text: str) -> bool:
    """在 `aria-label` **包含** group_aria 的分组里点一个按钮（分组标签带语言 flag 前缀）。"""
    return bool(
        driver.eval(
            "(() => { const g=[...document.querySelectorAll('[aria-label]')]"
            ".find(n => (n.getAttribute('aria-label')||'').includes(%r)); if(!g) return false;"
            " const b=[...g.querySelectorAll('button')].find(n => (n.innerText||'').trim().includes(%r));"
            " if(!b) return false; b.scrollIntoView({block:'center'}); b.click(); return true; })()"
            % (group_aria, text)
        )
    )


def ensure_target(driver: Chrome, text: str) -> bool:
    """确保某门目标语言处于选中态（草稿会预填已保存的目标语言，直接点会取消选中）。"""
    return bool(
        driver.eval(
            "(() => { const b=[...document.querySelectorAll('button')]"
            ".find(n => (n.innerText||'').trim().includes(%r));"
            " if(!b) return false; if (b.getAttribute('aria-pressed') === 'true') return true;"
            " b.scrollIntoView({block:'center'}); b.click(); return true; })()" % text
        )
    )


def press_enter(driver: Chrome, selector: str) -> bool:
    return bool(
        driver.eval(
            "(() => { const el=document.querySelector(%r); if(!el) return false;"
            " el.focus(); el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',"
            " keyCode:13, which:13, bubbles:true})); return true; })()" % selector
        )
    )


def localStorage(driver: Chrome, key: str) -> Any:
    raw = driver.eval(f"window.localStorage.getItem({json.dumps(key)})")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def api_profile(identity: dict[str, Any]) -> dict[str, Any]:
    headers = {"X-User-Id": str(identity["user_id"]), "X-User-Token": str(identity["token"])}
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{BASE}/api/v3/profile", headers=headers)
        if response.status_code == 401 and identity.get("device_key"):
            # 服务端每次 register 都会轮换 token；页面 reload 后旧 token 会失效，
            # 用 device_key 重新取一个（与客户端 ensureIdentity 行为一致）。
            fresh = client.post(
                BASE + "/api/v3/auth/register",
                json={"device_key": identity["device_key"]},
            ).json()
            headers = {"X-User-Id": str(fresh["user_id"]), "X-User-Token": str(fresh["token"])}
            response = client.get(f"{BASE}/api/v3/profile", headers=headers)
        response.raise_for_status()
        return response.json()


def fill_steps(driver: Chrome, nickname: str, *, custom_interest: str = "") -> None:
    driver.wait_text("Get to know you", timeout=40)
    check("Step1 标题存在", True)
    check("Step1 昵称输入存在", bool(driver.eval("!!document.querySelector('#se-onb-name')")))
    check("Step1 母语选择存在", bool(driver.eval("!!document.querySelector('#se-onb-native')")))
    driver.set_value("#se-onb-name", nickname)
    driver.select_option("#se-onb-native", "zh")
    time.sleep(0.2)
    driver.click_text("继续")
    driver.wait_text("Your language journey", timeout=25)
    check("Step2 标题存在", True)
    check("Step2 选中英语目标语言", ensure_target(driver, "英语"))
    check("Step2 选中德语目标语言", ensure_target(driver, "德语"))
    time.sleep(0.5)
    check("Step2 德语独立水平面板", bool(driver.eval("!!document.querySelector('[aria-label*=\"德语 的水平\"]')")))
    check("Step2 英语独立水平面板", bool(driver.eval("!!document.querySelector('[aria-label*=\"英语 的水平\"]')")))
    check("Step2 德语水平设为「刚开始」", click_in_group(driver, "德语 的水平", "刚开始"))
    driver.click_text("继续")
    driver.wait_text("Things you love", timeout=25)
    check("Step3 标题存在", True)
    driver.click_text("科技")
    driver.click_text("游戏")
    driver.click_text("编程")
    if custom_interest:
        driver.set_value('input[aria-label="自定义兴趣标签"]', custom_interest)
        pressed = press_enter(driver, 'input[aria-label="自定义兴趣标签"]')
        check("Step3 自定义兴趣回车添加", pressed and custom_interest in driver.text())
    driver.click_text("偶尔纠正明显的错误")
    time.sleep(0.2)


def save_and_wait(driver: Chrome, label: str = "开始认识朋友") -> bool:
    driver.click_text(label)
    deadline = time.time() + 60
    while time.time() < deadline and not app_ready(driver):
        time.sleep(0.4)
    return app_ready(driver)


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
#: Chrome 自身网络层噪声（profile 复用后的磁盘缓存错误 / 主动导航中断），不是页面 bug
BENIGN_NETWORK = ("ERR_CACHE_READ_FAILURE", "ERR_ABORTED")


def app_errors(driver: Chrome, *, extra: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return driver.js_errors(ignore=BENIGN_NETWORK + extra)


def page_state(driver: Chrome) -> dict[str, Any]:
    """失败时把真实页面状态写进报告，避免只看到空 detail。"""
    try:
        return {
            "onboarding": in_onboarding(driver),
            "app": app_ready(driver),
            "device_key": driver.eval("localStorage.getItem('speakeasy.device_key.v1')"),
            "identity": (driver.eval("localStorage.getItem('speakeasy.identity.v1')") or "")[:160],
            "text": driver.text()[:160].replace("\n", " | "),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def force_identity(driver: Chrome, device_key: str, *, retries: int = 3) -> bool:
    """让浏览器以指定 device_key 启动（模拟「老用户在新设备登录」）。

    旧文档里可能还有 in-flight 的 register 写入，会把我们刚写的 key 覆盖回随机值；
    所以每轮写完后先回读校验，再 reload，reload 后再确认 identity.device_key 一致。
    """
    for _attempt in range(retries):
        driver.eval(
            # localStorage 里存的是 JSON 编码值（前端 writeLocal = JSON.stringify），
            # 这里必须写 JSON.stringify(key)，否则 JSON.parse 失败 → App 生成新身份。
            "localStorage.setItem('speakeasy.device_key.v1', JSON.stringify(%s));"
            "localStorage.removeItem('speakeasy.identity.v1'); true" % json.dumps(device_key)
        )
        time.sleep(0.4)
        stored = driver.eval("localStorage.getItem('speakeasy.device_key.v1')")
        if str(stored or "").strip('"') != device_key:
            continue
        driver.navigate(BASE + "/")
        deadline = time.time() + 20
        while time.time() < deadline:
            identity = localStorage(driver, "speakeasy.identity.v1") or {}
            if str(identity.get("device_key") or "") == device_key:
                return True
            time.sleep(0.3)
    return False


def scenario_main(driver: Chrome) -> None:
    driver.navigate(BASE + "/")
    seen = wait_app_or_onboarding(driver)
    check("全新用户首次进入显示 Onboarding", seen == "onboarding", seen)
    check("首次进入不渲染主界面（无 Tab/侧栏）", not app_ready(driver))
    shot(driver, "01-onboarding-step1-desktop")
    nickname = "AlexE2E"
    fill_steps(driver, nickname, custom_interest="Board games")
    ok, info = no_horizontal_overflow(driver)
    check("Step3 桌面无横向溢出", ok, info)
    shot(driver, "02-onboarding-step3-desktop")
    check("保存成功后进入主界面", save_and_wait(driver), driver.text()[:200])
    shot(driver, "03-main-after-onboarding-desktop")
    identity = localStorage(driver, "speakeasy.identity.v1") or {}
    check("身份已持久化到 localStorage", bool(identity.get("user_id")))
    state = api_profile(identity)
    profile = state["profile"]
    check("后端标记建档完成", state["setup"]["completed"] is True, state["setup"])
    check("昵称落库", profile.get("nickname") == nickname, profile.get("nickname"))
    check("母语落库", profile.get("native_language") == "zh", profile.get("native_language"))
    check("多目标语言落库", profile.get("target_languages") == ["en", "de"], profile.get("target_languages"))
    fact = profile.get("fact_sheet") or {}
    levels = (fact.get("language_profile") or {}).get("levels") or {}
    check("每门语言独立水平落库", levels.get("en") == "能聊几句" and levels.get("de") == "刚开始", levels)
    interests = fact.get("interests") or []
    check("预设兴趣落库", {"Technology", "Gaming", "Programming"} <= set(interests), interests)
    check("自定义兴趣落库", "Board games" in [str(x) for x in interests], interests)
    check(
        "纠错偏好落库",
        fact.get("communication_preference") == "occasional",
        fact.get("communication_preference"),
    )
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    check("刷新后不再出现引导", app_ready(driver) and not in_onboarding(driver))
    shot(driver, "04-reload-no-onboarding")
    driver.click_text("我")
    time.sleep(0.8)
    check("「我」页面显示昵称", nickname in driver.text(), driver.text()[:160])
    shot(driver, "05-me-profile-desktop")
    errs = app_errors(driver)
    http_bad = driver.http_errors()
    check("主流程无 console error", not errs, errs[:5])
    check("主流程无 HTTP 4xx/5xx", not http_bad, [f"{x['status']} {x['url']}" for x in http_bad][:5])


def scenario_reopen(driver: Chrome) -> None:
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    state = page_state(driver)
    check("关闭浏览器后再次进入不出现引导", app_ready(driver) and not in_onboarding(driver), state)
    shot(driver, "06-reopen-no-onboarding")


def scenario_legacy(driver: Chrome) -> None:
    with httpx.Client(timeout=20) as client:
        device_key = "legacy-" + uuid.uuid4().hex
        reg = client.post(BASE + "/api/v3/auth/register", json={"device_key": device_key}).json()
        headers = {"X-User-Id": reg["user_id"], "X-User-Token": reg["token"]}
        client.put(
            BASE + "/api/v3/profile",
            headers=headers,
            json={
                "nickname": "LegacyAlex",
                "native_language": "zh",
                "target_languages": ["en"],
                "levels": {"en": "日常对话"},
                "complete_setup": True,
            },
        )
    # 先在同源 JSON 页面写身份，再进入 App：避免「首屏注册请求在途覆盖 device_key」的竞态。
    driver.navigate(BASE + "/api/v3/health")
    driver.eval(
        # 前端 writeLocal 用 JSON.stringify，读写是对称的；直接写裸字符串会导致
        # JSON.parse 失败，App 就当成「没有设备身份」重新注册（假失败）。
        "localStorage.setItem('speakeasy.device_key.v1', JSON.stringify(%s));"
        "localStorage.removeItem('speakeasy.identity.v1'); true" % json.dumps(device_key)
    )
    driver.navigate(BASE + "/")
    driver.wait_js("!!document.querySelector('.se-onb-screen, .w4-app')", timeout=60)
    deadline = time.time() + 30
    switched = False
    while time.time() < deadline:
        identity = localStorage(driver, "speakeasy.identity.v1") or {}
        if str(identity.get("device_key") or "") == device_key:
            switched = True
            break
        time.sleep(0.3)
    check("浏览器已切换到老用户身份", switched, page_state(driver))
    deadline = time.time() + 30
    while time.time() < deadline and not app_ready(driver):
        time.sleep(0.3)
    app_ok = app_ready(driver)
    check("老用户（已建档）直接进入主界面", app_ok and not in_onboarding(driver), page_state(driver))
    if not app_ok:
        shot(driver, "error-legacy")
    else:
        driver.click_text("我")
        time.sleep(0.8)
        check("老用户资料保留（LegacyAlex）", "LegacyAlex" in driver.text())
        shot(driver, "07-legacy-user-no-onboarding")


def scenario_mobile(driver: Chrome) -> None:
    driver.set_viewport(390, 844, mobile=True, scale=2)
    driver.navigate(BASE + "/")
    seen = wait_app_or_onboarding(driver)
    check("手机端新用户出现引导", seen == "onboarding", seen)
    driver.wait_text("Get to know you", timeout=40)
    ok, info = no_horizontal_overflow(driver)
    check("Step1 手机宽无横向溢出", ok, info)
    shot(driver, "08-onboarding-step1-mobile")
    driver.set_value("#se-onb-name", "MobileE2E")
    driver.select_option("#se-onb-native", "zh")
    driver.click_text("继续")
    driver.wait_text("Your language journey", timeout=25)
    ok, info = no_horizontal_overflow(driver)
    check("Step2 手机宽无横向溢出", ok, info)
    check("Step2 手机端选中英语", ensure_target(driver, "英语"))
    check("Step2 手机端选中日语", ensure_target(driver, "日语"))
    time.sleep(0.5)
    check("Step2 手机端出现分语言水平设置", bool(driver.eval("!!document.querySelector('[aria-label*=\"日语 的水平\"]')")))
    shot(driver, "09-onboarding-step2-mobile")
    driver.click_text("继续")
    driver.wait_text("Things you love", timeout=25)
    ok, info = no_horizontal_overflow(driver)
    check("Step3 手机宽无横向溢出", ok, info)
    shot(driver, "10-onboarding-step3-mobile")
    driver.set_viewport(320, 720, mobile=True, scale=2)
    time.sleep(0.5)
    ok, info = no_horizontal_overflow(driver)
    check("320px 极窄无横向溢出", ok, info)
    shot(driver, "11-onboarding-320px")
    check("手机端保存进入主界面", save_and_wait(driver))
    driver.set_viewport(390, 844, mobile=True, scale=2)
    time.sleep(0.6)
    ok, info = no_horizontal_overflow(driver)
    check("主界面手机宽无横向溢出", ok, info)
    shot(driver, "11b-main-mobile")


def scenario_offline(driver: Chrome) -> None:
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    fill_steps(driver, "OfflineE2E")
    driver.set_offline(True)
    time.sleep(0.4)
    driver.click_text("开始认识朋友")
    driver.wait_js(
        "(() => { const t=document.body.innerText; return t.includes('保存失败') || t.includes('检查网络'); })()",
        timeout=40,
    )
    check("断网保存显示错误提示", True)
    check("断网保存后仍停留在引导（未显示完成）", in_onboarding(driver))
    check("断网保存后仍在第 3 步", "Things you love" in driver.text())
    shot(driver, "12-onboarding-offline-error")
    driver.set_offline(False)
    time.sleep(0.6)
    driver.click_text("重试")
    deadline = time.time() + 60
    while time.time() < deadline and not app_ready(driver):
        time.sleep(0.4)
    check("恢复网络后重试成功进入主界面", app_ready(driver), driver.text()[:160])
    identity = localStorage(driver, "speakeasy.identity.v1") or {}
    state = api_profile(identity)
    check("重试成功后后端标记建档完成", state["setup"]["completed"] is True, state["setup"])
    shot(driver, "13-onboarding-retry-success")


def scenario_isolation(driver: Chrome, nickname: str) -> None:
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    fill_steps(driver, nickname)
    check(f"{nickname} 保存成功", save_and_wait(driver))
    identity = localStorage(driver, "speakeasy.identity.v1") or {}
    TMP.mkdir(exist_ok=True)
    (TMP / f"isolation-{nickname}.json").write_text(json.dumps(identity), encoding="utf-8")
    check(f"{nickname} 身份已保存", bool(identity.get("user_id")))
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    check(f"{nickname} 刷新后无引导", app_ready(driver))


def scenario_isolation_check(_driver: Chrome | None) -> None:
    files = sorted(TMP.glob("isolation-*.json"))
    identities = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    check("两个隔离用户身份都已保存", len(identities) >= 2, len(identities))
    if len(identities) >= 2:
        a, b = identities[0], identities[1]
        pa = api_profile(a)["profile"]
        pb = api_profile(b)["profile"]
        check("两个用户 user_id 不同", a.get("user_id") != b.get("user_id"))
        check("用户 A 资料正确", pa.get("nickname") in ("UserAlpha", "UserBeta"), pa.get("nickname"))
        check("用户 B 资料正确", pb.get("nickname") in ("UserAlpha", "UserBeta"), pb.get("nickname"))
        check("A/B 昵称未互相覆盖", pa.get("nickname") != pb.get("nickname"))


# --------------------------------------------------------------------------- #
# 运行与汇总
# --------------------------------------------------------------------------- #
def scenario_chat(driver: Chrome) -> None:
    """真实浏览器聊天：建档 → 加好友 → 打开会话 → 发消息 → 等真实 LLM 回复。"""
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    fill_steps(driver, "ChatE2E")
    check("聊天场景：建档完成进入主界面", save_and_wait(driver), page_state(driver))
    identity = localStorage(driver, "speakeasy.identity.v1") or {}
    headers = {
        "X-User-Id": str(identity.get("user_id") or ""),
        "X-User-Token": str(identity.get("token") or ""),
    }
    # 选一个可用的英文人物（走 API，本机与远端一致；guest/adopted 会 404 已被排除）
    with httpx.Client(timeout=30) as client:
        try:
            persona_id, persona_name = _pick_english_persona(client)
        except Exception as exc:  # noqa: BLE001
            check("聊天场景：找到可用人物", False, f"{type(exc).__name__}: {exc}")
            return
        response = client.post(
            f"{BASE}/api/v3/personas/{persona_id}/friend", headers=headers, json={}
        )
    friend_id = ""
    if response.status_code == 200:
        friend_id = str(response.json().get("friend_id") or "")
    check("聊天场景：加好友成功", bool(friend_id), response.status_code)
    if not friend_id:
        return
    # 重新加载让 bootstrap 带上新好友（否则前端的 friends 里没有这个人，会话页打不开）
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    time.sleep(0.6)
    # reload 会重新 register 并轮换 token：必须重新读最新身份再调 API
    identity = localStorage(driver, "speakeasy.identity.v1") or identity
    headers = {
        "X-User-Id": str(identity.get("user_id") or ""),
        "X-User-Token": str(identity.get("token") or ""),
    }
    # hash 路由：同文档内跳转（不能走 driver.navigate，它等价于整页加载）
    driver.eval("window.location.hash = %s; true" % json.dumps(f"#/chat/{friend_id}"))
    driver.wait_js("!!document.querySelector('.w4-composer-input')", timeout=60)
    check("聊天场景：打开会话页", True)
    driver.set_value(".w4-composer-input", "hey! how's your day going?")
    time.sleep(0.3)
    driver.click_selector('[aria-label="发送"]')
    # 用户气泡立刻出现（乐观消息）
    driver.wait_js("document.querySelectorAll('.w4-msg.is-me .w4-bubble').length >= 1", timeout=20)
    check("聊天场景：用户消息已显示", True)
    # 等真实回复（LLM）：声气泡本身只显示时长，文字在 transcript 里，
    # 因此「等 DOM 出现好友气泡」+「用 API 读 transcript / audio」一起断言。
    deadline = time.time() + 180
    friend_bubbles = 0
    while time.time() < deadline:
        try:
            friend_bubbles = int(
                driver.eval(
                    "document.querySelectorAll('.w4-msg:not(.is-me) .w4-bubble:not(.is-typing)').length"
                )
                or 0
            )
        except Exception:  # noqa: BLE001
            friend_bubbles = 0
        if friend_bubbles > 0:
            break
        time.sleep(1.0)
    check("聊天场景：出现回复气泡（非正在输入占位）", friend_bubbles > 0, friend_bubbles)
    reply = ""
    audio_url = ""
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"{BASE}/api/v2/friends/{friend_id}/messages", headers=headers
            )
        response.raise_for_status()
        messages = response.json().get("messages") or []
        friend_messages = [m for m in messages if str(m.get("sender")) == "friend"]
        if friend_messages:
            last = friend_messages[-1]
            reply = str(last.get("transcript") or "")
            audio_url = str(last.get("audio_url") or "")
    except Exception as exc:  # noqa: BLE001
        check("聊天场景：读取回复原文", False, f"{type(exc).__name__}: {exc}")
    check(f"聊天场景：收到 {persona_name} 的真实回复", len(reply) > 2, reply[:200])
    letters = [ch for ch in reply if ch.isalpha()]
    latin = sum(1 for ch in letters if ch.isascii()) / len(letters) if letters else 0.0
    check("聊天场景：回复是英文（目标语言）", latin >= 0.7, {"reply": reply[:160], "latin": round(latin, 2)})
    check("聊天场景：回复带语音（同一人物音色链路）", bool(audio_url), audio_url)
    # 实时语音通话链路：能创建通话票据（不真正拨号），随后挂断清理
    try:
        with httpx.Client(timeout=30) as client:
            ticket = client.post(
                f"{BASE}/api/v3/calls",
                headers=headers,
                json={"friend_id": friend_id, "request_id": f"verify-{uuid.uuid4().hex[:12]}"},
            )
        ok = ticket.status_code == 200 and bool(ticket.json().get("ticket"))
        check("聊天场景：实时通话票据可创建", ok, ticket.status_code)
        if ticket.status_code == 200:
            call_id = str(ticket.json().get("call_id") or "")
            if call_id:
                with httpx.Client(timeout=30) as client:
                    client.delete(f"{BASE}/api/v3/calls/{call_id}", headers=headers)
    except Exception as exc:  # noqa: BLE001
        check("聊天场景：实时通话票据可创建", False, f"{type(exc).__name__}: {exc}")
    shot(driver, "14-chat-real-reply")
    errs = app_errors(driver)
    http_bad = [e for e in driver.http_errors() if "/audio" not in str(e.get("url")) and "/speech" not in str(e.get("url"))]
    check("聊天场景：无 console error", not errs, errs[:5])
    check("聊天场景：无 HTTP 4xx/5xx", not http_bad, [f"{x['status']} {x['url']}" for x in http_bad][:5])


def _pick_english_persona(client: httpx.Client) -> tuple[str, str]:
    """从线上/本机 API 里挑一个可用的英文人物（不依赖本地 DB，远端也能用）。"""
    response = client.get(f"{BASE}/api/v3/personas", params={"limit": 60}, timeout=30)
    response.raise_for_status()
    items = response.json().get("items") or []
    for item in items:
        if str(item.get("language") or "en") != "en":
            continue
        persona_id = str(item.get("id") or "")
        detail = client.get(f"{BASE}/api/v3/personas/{persona_id}", timeout=30)
        if detail.status_code == 200:
            return persona_id, str(item.get("nickname") or "")
    raise RuntimeError("没有可用的英文人物")


def scenario_compat(driver: Chrome) -> None:
    """兼容性巡检：动态流 / 人物主页 / 翻译 / 朋友圈 / TTS / 头像等既有功能不被破坏。"""
    driver.navigate(BASE + "/")
    wait_app_or_onboarding(driver)
    fill_steps(driver, "CompatE2E")
    check("兼容性：新用户建档完成", save_and_wait(driver), page_state(driver))
    identity = localStorage(driver, "speakeasy.identity.v1") or {}
    headers = {
        "X-User-Id": str(identity.get("user_id") or ""),
        "X-User-Token": str(identity.get("token") or ""),
    }
    with httpx.Client(timeout=60) as client:
        feed = client.get(f"{BASE}/api/v3/feed", params={"limit": 10}, headers=headers)
        check("兼容性：发现动态流可用", feed.status_code == 200, feed.status_code)
        feed_items = feed.json().get("items") or []
        check("兼容性：动态流有内容", len(feed_items) > 0, len(feed_items))
        summary = client.get(f"{BASE}/api/v3/feed/summary", headers=headers)
        check("兼容性：动态统计可用", summary.status_code == 200, summary.status_code)
        persona_id, persona_name = _pick_english_persona(client)
        profile = client.get(f"{BASE}/api/v3/personas/{persona_id}", headers=headers)
        check("兼容性：人物主页可用", profile.status_code == 200, profile.status_code)
        persona_payload = profile.json()
        first_message = str(persona_payload.get("first_message") or "")
        check("兼容性：人物主页含首条开场消息", bool(first_message), first_message[:60])
        posts = client.get(f"{BASE}/api/v3/personas/{persona_id}/posts", headers=headers)
        check("兼容性：人物动态可用", posts.status_code == 200, posts.status_code)
        moments = client.get(f"{BASE}/api/v2/moments", headers=headers)
        check("兼容性：朋友圈可用", moments.status_code == 200, moments.status_code)
        translate = client.post(
            f"{BASE}/api/v2/translate",
            headers=headers,
            json={"text": "This is a compatibility check.", "target": "zh"},
        )
        check("兼容性：翻译接口可用", translate.status_code == 200, translate.status_code)
        avatar = client.get(f"{BASE}/api/v3/avatar/probe-seed.svg")
        check(
            "兼容性：头像生成可用",
            avatar.status_code == 200 and "svg" in avatar.headers.get("content-type", ""),
            avatar.status_code,
        )
        persona_posts = [item for item in feed_items if item.get("kind") == "persona_post"]
        if persona_posts:
            speech = client.get(f"{BASE}/api/v3/persona-posts/{persona_posts[0]['id']}/speech", timeout=180)
            ok = speech.status_code == 200 and "audio" in str(speech.headers.get("content-type"))
            check("兼容性：动态朗读（TTS）可用", ok, {"status": speech.status_code, "len": len(speech.content)})
        # 浏览器侧：发现页 + 人物主页真实渲染
        driver.eval("window.location.hash = '#/discover'; true")
        time.sleep(2.0)
        discover_text = driver.text()
        check("兼容性：发现页可渲染", len(discover_text) > 50, discover_text[:120])
        shot(driver, "20-compat-discover")
        driver.eval("window.location.hash = %s; true" % json.dumps(f"#/persona/{persona_id}"))
        time.sleep(2.5)
        persona_text = driver.text()
        check(
            "兼容性：人物主页可渲染",
            persona_name in persona_text and len(persona_text) > 50,
            persona_text[:160],
        )
        # 首条开场消息当前由 API/Prompt 层提供（空聊天页 UI 展示属于后续优化，
        # 见交付报告「已知问题」）；这里只断言数据链路本身存在且非空。
        check(
            "兼容性：首条开场消息已由 API 提供",
            bool(first_message),
            first_message[:60],
        )
        shot(driver, "21-compat-persona")
    errs = app_errors(driver)
    check("兼容性：无 console error", not errs, errs[:5])


def run(scenario: str, port: int | None, nickname: str) -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    REPORT.mkdir(parents=True, exist_ok=True)
    started = time.time()
    driver: Chrome | None = None
    try:
        if scenario in ("isolation-check",):
            scenario_isolation_check(None)
        else:
            if port is None:
                raise SystemExit("需要 --port")
            driver = Chrome.attach(port)
            driver.clear_logs()
            if scenario == "main":
                scenario_main(driver)
            elif scenario == "reopen":
                scenario_reopen(driver)
            elif scenario == "legacy":
                scenario_legacy(driver)
            elif scenario == "mobile":
                scenario_mobile(driver)
            elif scenario == "offline":
                scenario_offline(driver)
            elif scenario == "isolation":
                scenario_isolation(driver, nickname)
            elif scenario == "chat":
                scenario_chat(driver)
            elif scenario == "compat":
                scenario_compat(driver)
            else:
                raise SystemExit(f"未知 scenario: {scenario}")
            errs = app_errors(driver, extra=("ERR_INTERNET_DISCONNECTED",) if scenario == "offline" else ())
            check(f"[{scenario}] 全程无 console error", not errs, errs[:5])
    except Exception as exc:  # noqa: BLE001
        check(f"[{scenario}] 未抛异常", False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1200:]}")
        if driver is not None:
            try:
                shot(driver, f"error-{scenario}")
            except Exception:  # noqa: BLE001
                pass
    finally:
        # 优雅关闭浏览器：让 Chrome 把 localStorage/LevelDB 落盘，
        # 否则「关掉浏览器再进入」会读到空 profile（这是验证脚本的假失败来源）。
        if driver is not None:
            try:
                driver.call("Browser.close", timeout=10)
            except Exception:  # noqa: BLE001
                pass
    passed = sum(1 for item in results if item["ok"])
    failed = [item for item in results if not item["ok"]]
    payload = {
        "kind": f"local-web-{scenario}-verification",
        "scenario": scenario,
        "generated_at": datetime.now().astimezone().isoformat(),
        "base": BASE,
        "duration_s": round(time.time() - started, 1),
        "checks_total": len(results),
        "checks_passed": passed,
        "checks_failed": len(failed),
        "passed": not failed,
        "results": results,
    }
    out = REPORT / f"{PREFIX}-{scenario}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[{scenario}] {passed}/{len(results)} 通过；失败 {len(failed)}；报告 {out.relative_to(ROOT)}")
    return 0 if not failed else 1


def summary(_args: argparse.Namespace) -> int:
    """把各 scenario 报告合并成最终一份（本机 Web 验证闸门证据）。"""
    latest: dict[str, dict[str, Any]] = {}
    for path in sorted(REPORT.glob(f"{PREFIX}-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        scenario = str(data.get("scenario") or "")
        if not scenario:
            continue
        latest[scenario] = {
            "report": str(path.relative_to(ROOT)).replace("\\", "/"),
            "checks_total": data.get("checks_total"),
            "checks_passed": data.get("checks_passed"),
            "checks_failed": data.get("checks_failed"),
            "passed": data.get("passed"),
            "failed_checks": [item for item in data.get("results", []) if not item.get("ok")],
        }
    expected = EXPECTED_DEFAULT
    missing = [name for name in expected if name not in latest]
    all_passed = bool(latest) and not missing and all(value["passed"] for value in latest.values())
    payload = {
        "kind": "local-web-verification-summary",
        "generated_at": datetime.now().astimezone().isoformat(),
        "scenarios": latest,
        "missing_scenarios": missing,
        "all_passed": all_passed,
        "screenshots": sorted(
            str(path.relative_to(ROOT)).replace("\\", "/") for path in SHOTS.glob("*.png")
        ),
    }
    out = REPORT / f"{PREFIX}-verification-summary.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"all_passed": all_passed, "missing": missing,
                      "scenarios": {k: v["passed"] for k, v in latest.items()}},
                     ensure_ascii=False, indent=2))
    print("报告：", out.relative_to(ROOT))
    return 0 if all_passed else 1


def main() -> int:
    global BASE, PREFIX, EXPECTED_DEFAULT, SHOTS, REPORT, TMP
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="main")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--nickname", default="UserAlpha")
    parser.add_argument("--base", default="", help="被测站点（本机 127.0.0.1:8000 / 远端公网地址）")
    parser.add_argument("--prefix", default="", help="报告前缀（local-web / remote-web）")
    parser.add_argument(
        "--expected",
        default="",
        help="summary 需要包含哪些场景，逗号分隔（默认本机 8 个场景）",
    )
    args = parser.parse_args()
    if args.base:
        BASE = args.base.rstrip("/")
    if args.prefix:
        PREFIX = args.prefix
        SHOTS = ROOT / "artifacts" / f"{args.prefix}-screenshots"
        REPORT = ROOT / "artifacts" / f"{args.prefix}-reports"
    if args.expected:
        EXPECTED_DEFAULT = [item.strip() for item in args.expected.split(",") if item.strip()]
    if args.scenario == "summary":
        return summary(args)
    return run(args.scenario, args.port, args.nickname)


if __name__ == "__main__":
    raise SystemExit(main())
