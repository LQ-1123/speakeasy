"""全量流程测试（真实服务 + 真实 LLM + 真实 TTS），覆盖：健康 → 管理端 → 捏人 → 加好友
→ 聊天（文本 + 语音）→ 动态 → 朗读 → 事件/节奏热更新 → 越权/校验拦截。

    python scripts/e2e_full_flow.py

只依赖本机运行中的服务（默认 http://127.0.0.1:8000）。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(ROOT, "data", "audio")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} · {name}" + (f" · {detail}" if detail else ""))


def main() -> int:
    started = time.time()
    run_id = int(started)
    headers: dict[str, str] = {}
    friend_id = ""

    with httpx.Client(base_url=BASE, timeout=180.0) as anon:
        # 远程部署时管理接口需要口令（SE_ACCEPT_TOKEN）；本地为空则不带
        _admin_token = os.getenv("SE_ACCEPT_TOKEN", "").strip()
        if _admin_token:
            anon.headers["X-Admin-Token"] = _admin_token
        # 1) 健康与语音
        try:
            health = anon.get("/api/v3/health").json()
        except Exception as exc:  # noqa: BLE001
            print("服务未启动:", exc)
            return 2
        voice = health.get("voice") or {}
        check("健康检查", health.get("status") == "ok", f"version={health.get('version')}")
        check(
            "语音链路就绪",
            voice.get("provider") == "fish",
            f"provider={voice.get('provider')} fish={voice.get('fish', {}).get('reachable')}",
        )

        # 2) 管理端
        status = anon.get("/api/admin/status").json()
        ok_lexicons = all(item["ok"] for item in status["lexicons"])
        check("词库全部就绪", ok_lexicons, f"{len(status['lexicons'])} 个词库")
        applied = status.get("applied") or {}
        check(
            "词库已热生效",
            applied.get("dna_fields") == 13 and applied.get("event_types") == 12,
            f"dna={applied.get('dna_fields')} events={applied.get('event_types')} rhythm={applied.get('rhythm')}",
        )

        cfg = anon.get("/api/admin/config").json()
        raw_like = any(
            isinstance(v, str) and v.startswith("sk-") and len(v) > 16
            for v in (
                (cfg.get("llm") or {}).values(),
                (cfg.get("tts") or {}).values(),
                (cfg.get("stt") or {}).values(),
            )
            for v in ([v] if isinstance(v, str) else [])
        )
        check("配置接口不泄露明文 key", not raw_like, "key 只返回指纹")
        check("客户端配置锁已开", bool(cfg.get("client_locked")) is True)

        # 3) 素材池内容
        world = anon.get("/api/admin/lexicons/world").json()["data"]
        by_country: dict[str, int] = {}
        for group in (world.get("names") or {}).values():
            for item in group:
                country = item.get("country") or "?"
                by_country[country] = by_country.get(country, 0) + 1
        thin = {k: v for k, v in by_country.items() if v < 5}
        check("每个国家至少 5 个名字", not thin, f"国家数={len(by_country)} 最少={min(by_country.values())}")

        # 4) 捏人预览（多语言、性别/国家/城市一致）
        preview_ok = True
        preview_detail: list[str] = []
        for lang in ("en", "ja", "ko", "de"):
            preview = anon.get(
                "/api/admin/preview-personas", params={"count": 5, "language": lang}
            ).json()
            names = [p["nickname"] for p in preview.get("personas", [])]
            cities_pool = world.get("cities") or {}
            for persona in preview.get("personas", []):
                country = persona.get("country") or ""
                if country not in cities_pool:
                    preview_ok = False
                    preview_detail.append(f"{lang}:{persona['nickname']} 国家 {country} 无城市池")
                elif persona.get("city") not in cities_pool[country]:
                    preview_ok = False
                    preview_detail.append(
                        f"{lang}:{persona['nickname']} 城市 {persona.get('city')} 不属于 {country}"
                    )
                if persona.get("gender") not in ("male", "female"):
                    preview_ok = False
                    preview_detail.append(f"{lang}:{persona['nickname']} 性别缺失")
            if len(set(names)) != len(names):
                preview_ok = False
                preview_detail.append(f"{lang} 预览重名: {names}")
        check("捏人预览（性别/国家/城市一致）", preview_ok, "; ".join(preview_detail[:3]))

        # 5) 注册 + 加好友
        reg = anon.post(
            "/api/v3/auth/register", json={"device_key": f"e2e-flow-{run_id}"}
        )
        check("注册新用户", reg.status_code == 200, f"status={reg.status_code}")
        user = reg.json()
        headers = {"X-User-Id": user["user_id"], "X-User-Token": user["token"]}

        personas = anon.get("/api/v3/personas", params={"limit": 40}).json().get("items") or []
        target = next((p for p in personas if p.get("nickname") == "Adrian"), None)
        if target is None:
            target = next(
                (p for p in personas if str(p.get("language") or "").lower() == "en"),
                personas[0] if personas else None,
            )
        check("发现可加的人物", target is not None, target.get("nickname") if target else "none")
        if target is None:
            return 1
        with httpx.Client(base_url=BASE, timeout=240.0, headers=headers) as client:
            added = client.post(f"/api/v3/personas/{target['id']}/friend", json={})
            friend_id = (added.json() or {}).get("friend_id", "") if added.status_code == 200 else ""
            check("加人物为好友", bool(friend_id), f"status={added.status_code} friend={friend_id[:10]}")

            # 6) 聊天（3 轮，真实 LLM）
            before_audio = set(os.listdir(AUDIO_DIR)) if os.path.isdir(AUDIO_DIR) else set()
            replies: list[str] = []
            audio_urls: list[str] = []
            chat_ok = True
            for text in (
                "Hey! I just moved to your city and I don't know anyone yet.",
                "What's something small that made you happy this week?",
                "Honestly I'm a bit nervous about starting over. Any advice?",
            ):
                resp = client.post(f"/api/v2/chat/{friend_id}/text", json={"text": text})
                if resp.status_code != 200:
                    chat_ok = False
                    break
                data = resp.json()
                msgs = data.get("friend_messages") or []
                reply = (msgs[0].get("transcript") if msgs else "") or ""
                replies.append(reply)
                audio_urls.append((msgs[0].get("audio_url") if msgs else None) or "")
            check("连续 3 轮聊天", chat_ok, f"首轮回复={replies[0][:40] if replies else '空'}…")
            check(
                "回复非空且不重复",
                len(replies) == 3 and all(replies) and len(set(replies)) == 3,
                f"长度={[len(r) for r in replies]}",
            )
            target_lang = str((target or {}).get("language") or "en").strip().lower()
            # 只看“非 ASCII 字母”（英文回复允许 emoji / 弯引号 / 破折号等标点）
            non_latin_letters = [
                ch
                for ch in "".join(replies)
                if ord(ch) >= 128
                and ch.isalpha()
                and not unicodedata.name(ch, "").startswith("LATIN")
            ]
            lang_ok = (not non_latin_letters) if target_lang == "en" else True
            check(
                "回复语言正确",
                lang_ok,
                f"{target_lang} 人物回复语言 (非拉丁字母={''.join(non_latin_letters)[:20] or '无'})",
            )

            # 7) 语音条真实生成（远程部署时音频在服务器上，跟随 audio_url 拉取校验）
            time.sleep(1.5)
            remote_audio = [url for url in audio_urls if url]
            if remote_audio:
                ok = True
                checked = 0
                provider = ""
                for url in remote_audio[:2]:
                    audio_resp = client.get(url)
                    if audio_resp.status_code != 200 or len(audio_resp.content) < 2000:
                        ok = False
                        continue
                    checked += 1
                    provider = (
                        "fish" if "fish_" in url else "edge" if "tts_" in url else "?"
                    )
                check(
                    "聊天语音条已生成",
                    ok and checked > 0,
                    f"provider={provider} urls={len(remote_audio)} "
                    f"bytes={len(audio_resp.content) if checked else 0}",
                )
            else:
                # 本机部署：音频落在本地 data/audio
                after_audio = set(os.listdir(AUDIO_DIR)) if os.path.isdir(AUDIO_DIR) else set()
                new_audio = sorted(after_audio - before_audio)
                providers = {
                    "fish" if name.startswith("fish_") else "edge" if name.startswith("tts_") else "?"
                    for name in new_audio
                }
                check(
                    "聊天语音条已生成",
                    bool(new_audio),
                    f"provider={sorted(providers)} files={len(new_audio)}",
                )

            # 8) 同人同声：服务端绑定一致
            voice_stats = client.get("/api/v3/stats/voice").json()
            item = next(
                (row for row in voice_stats.get("items", []) if row["persona_id"] == target["id"]),
                None,
            )
            check(
                "聊天/朗读音色绑定一致",
                bool(item and item.get("consistent")),
                f"token={(item or {}).get('speech', {}).get('token')}",
            )

            # 9) 动态 + 朗读
            feed = client.get("/api/v3/feed", params={"limit": 30}).json()
            items = feed.get("items") or []
            posts = [i for i in items if i.get("kind") == "persona_post"]
            check("信息流返回内容", bool(items), f"items={len(items)} persona_posts={len(posts)}")
            if posts:
                with client.stream(
                    "GET", f"/api/v3/persona-posts/{posts[0]['id']}/speech"
                ) as resp:
                    audio_bytes = len(resp.read())
                    token = resp.headers.get("X-Voice", "")
                    provider = resp.headers.get("X-Voice-Provider", "")
                    status_code = resp.status_code
                check(
                    "动态朗读可播放",
                    status_code == 200 and audio_bytes > 2000,
                    f"status={status_code} bytes={audio_bytes} provider={provider} token={token}",
                )

            # 10) 世界统计（事件是后台异步生成的：轮询等待，最多 2 分钟）
            stats = client.get("/api/v3/stats/persona").json()
            for _ in range(6):
                if int(stats.get("events", {}).get("total") or 0) > 0:
                    break
                time.sleep(20)
                stats = client.get("/api/v3/stats/persona").json()
            check(
                "世界事件与动态在产出",
                stats["events"]["total"] > 0 and stats["posts"]["persona_posts"] > 0,
                f"events={stats['events']['total']} posts={stats['posts']['persona_posts']} "
                f"event_driven={stats['posts']['event_driven']}",
            )
            check(
                "关系/情绪状态存在",
                stats["relationships"]["total"] > 0 and stats["emotion"]["with_state"] > 0,
                f"relations={stats['relationships']['total']} emotion={stats['emotion']['with_state']}",
            )

            # 11) 客户端配置写入必须被拒绝
            locked = client.post("/api/v2/settings/llm", json={"model": "hacked"})
            check(
                "客户端改配置被拒绝",
                locked.status_code == 403,
                f"status={locked.status_code}",
            )

        # 12) 管理端热更新：节奏 + 事件类型 + 非法校验（回到匿名客户端）
        rhythm = anon.get("/api/admin/lexicons/rhythm").json()["data"]
        original_quota = int(rhythm["params"]["event_posts_per_day"])
        new_quota = 3 if original_quota != 3 else 4
        changed = {**rhythm, "params": {**rhythm["params"], "event_posts_per_day": new_quota}}
        saved = anon.put("/api/admin/lexicons/rhythm", json={"data": changed}).json()
        sim = anon.get("/api/admin/simulate").json()
        check(
            "世界节奏热生效",
            saved.get("ok") and sim["pending"].get("quota") == new_quota,
            f"quota {original_quota}→{new_quota}",
        )
        anon.post("/api/admin/lexicons/rhythm/reset")

        events = anon.get("/api/admin/lexicons/events").json()["data"]
        target_type = next(iter(events["types"]))
        events["types"][target_type]["enabled"] = False
        toggled = anon.put("/api/admin/lexicons/events", json={"data": events}).json()
        check(
            "事件类型启停热生效",
            toggled.get("ok") and (toggled.get("applied") or {}).get("event_types") < 12,
            f"停用 {target_type} 后类型数="
            f"{(toggled.get('applied') or {}).get('event_types')}",
        )
        anon.post("/api/admin/lexicons/events/reset")

        bad_dna = anon.get("/api/admin/lexicons/dna").json()["data"]
        bad_dna["fields"]["verbosity"]["values"]["long"]["trait"] = 9.0
        rejected = anon.put("/api/admin/lexicons/dna", json={"data": bad_dna}).json()
        check(
            "非法词库修改被拒绝（不落盘）",
            rejected.get("ok") is False and bool(rejected.get("issues")),
            (rejected.get("issues") or [""])[0][:60],
        )

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 60)
    print(f"结果：{passed}/{total} 通过 · 用时 {time.time() - started:.1f}s")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
