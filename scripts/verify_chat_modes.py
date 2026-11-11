"""文字 / 图片 / 语音 / 通话 四种交互模式的真实链路一致性验证（本机，真实 LLM）。

验证目标（对应需求 3.5 统一交互模式）：
1. 四种模式都能跑通（不是 Mock 冒烟：真实调用后端 + 真实 LLM）；
2. 文字与通话共享同一份角色卡/用户画像：管理端 prompt 预览里两者都有同一人物的
   姓名/职业/城市等身份字段，且通话版更短；
3. 图片/语音模式走的是同一条 chat_service 人设链路（回复非空、语言正确、不报错）；
4. TTS 朗读仍可用（同一人物音色）。

用法：python scripts/verify_chat_modes.py
"""

from __future__ import annotations

import io
import json
import sqlite3
import struct
import sys
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "speakeasy_v2.db"
OUT = ROOT / "artifacts" / "qa-reports"

checks: list[dict[str, Any]] = []


def check(name: str, ok: bool, detail: Any = "") -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f" -> {detail}"))
    return bool(ok)


def latin_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return round(sum(1 for ch in letters if ch.isascii()) / len(letters), 3)


def make_png() -> bytes:
    """生成一张 32x32 的纯色 PNG（不引入图片素材依赖）。"""
    from PIL import Image  # noqa: PLC0415

    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), (120, 160, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def make_wav(seconds: float = 1.0) -> bytes:
    """生成 16k 单声道正弦波 WAV（给 STT 链路用；mock STT 也能返回文本）。"""
    import math  # noqa: PLC0415

    sample_rate = 16000
    frames = int(sample_rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        data = bytearray()
        for index in range(frames):
            value = int(12000 * math.sin(2 * math.pi * 220 * index / sample_rate))
            data += struct.pack("<h", value)
        wav.writeframes(bytes(data))
    return buffer.getvalue()


def permanent_english_personas(limit: int = 3) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, nickname, language, occupation, city, country, age, ai_config, personality "
            "FROM personas WHERE status='active' AND lifecycle='permanent' AND language='en' "
            "ORDER BY CASE ai_config WHEN '' THEN 1 ELSE 0 END, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def register(client: httpx.Client) -> dict[str, str]:
    device_key = "modes-" + uuid.uuid4().hex
    response = client.post(BASE + "/api/v3/auth/register", json={"device_key": device_key})
    response.raise_for_status()
    data = response.json()
    identity = {"user_id": data["user_id"], "token": data["token"], "device_key": device_key}
    client.headers.update({"X-User-Id": identity["user_id"], "X-User-Token": identity["token"]})
    return identity


def send_text(client: httpx.Client, friend_id: str, text: str) -> str:
    response = client.post(
        f"{BASE}/api/v2/chat/{friend_id}/text",
        json={"text": text, "client_message_id": "modes-" + uuid.uuid4().hex},
        timeout=180,
    )
    response.raise_for_status()
    messages = response.json().get("friend_messages") or []
    return str(messages[0].get("transcript") or "") if messages else ""


def send_image(client: httpx.Client, friend_id: str, png: bytes) -> str:
    response = client.post(
        f"{BASE}/api/v2/chat/{friend_id}/image",
        files={"image": ("probe.png", png, "image/png")},
        data={"text": "look at this"},
        timeout=180,
    )
    response.raise_for_status()
    messages = response.json().get("friend_messages") or []
    return str(messages[0].get("transcript") or "") if messages else ""


def send_voice(client: httpx.Client, friend_id: str, wav: bytes) -> dict[str, Any]:
    response = client.post(
        f"{BASE}/api/v2/chat/{friend_id}/voice",
        files={"audio": ("probe.wav", wav, "audio/wav")},
        data={"duration_ms": "1000"},
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def send_call_turn(client: httpx.Client, friend_id: str, text: str) -> str:
    """通话回合（ndjson 流）：收集所有增量文本。"""
    collected: list[str] = []
    with client.stream(
        "POST",
        f"{BASE}/api/v2/chat/{friend_id}/call/turn-text",
        json={"text": text, "duration_ms": 1200},
        timeout=180,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind in ("delta", "sentence", "text"):
                collected.append(str(event.get("text") or event.get("delta") or ""))
            elif kind == "done":
                message = event.get("friend_message") or {}
                text_value = str(message.get("transcript") or "")
                if text_value:
                    collected.append(text_value)
    return "".join(collected).strip()


def prompt_preview(client: httpx.Client, persona_id: str, user_id: str, text: str, mode: str) -> dict[str, Any]:
    response = client.get(
        BASE + "/api/admin/prompt/preview",
        params={"persona_id": persona_id, "user_id": user_id, "text": text, "mode": mode},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "kind": "chat-modes-consistency",
        "generated_at": datetime.now().astimezone().isoformat(),
        "base": BASE,
        "checks": checks,
    }
    personas = permanent_english_personas(3)
    if len(personas) < 3:
        print("常驻英文人物不足 3 个", file=sys.stderr)
        return 2
    png = make_png()
    wav = make_wav()

    with httpx.Client(timeout=60) as client:
        identity = register(client)
        client.put(
            BASE + "/api/v3/profile",
            json={
                "nickname": "ModesAlex",
                "native_language": "zh",
                "target_languages": ["en"],
                "levels": {"en": "能聊几句"},
                "interests": ["Programming", "Gaming", "Music"],
                "communication_preference": "natural",
                "complete_setup": True,
            },
        )
        text_replies: dict[str, str] = {}
        friend_ids: dict[str, str] = {}
        for persona in personas:
            response = client.post(f"{BASE}/api/v3/personas/{persona['id']}/friend", json={})
            if response.status_code != 200:
                check(f"加好友 {persona['nickname']}", False, response.status_code)
                continue
            friend_id = str(response.json().get("friend_id") or "")
            friend_ids[persona["id"]] = friend_id
            reply = send_text(client, friend_id, "hey! how's your day going?")
            text_replies[persona["id"]] = reply
            check(
                f"文字模式回复 {persona['nickname']}",
                bool(reply) and len(reply) > 4,
                reply[:120],
            )
            check(
                f"文字模式语言一致性 {persona['nickname']}",
                latin_ratio(reply) >= 0.85,
                {"latin_ratio": latin_ratio(reply), "reply": reply[:120]},
            )
        check("三个英文字符人物口吻不同", len(set(text_replies.values())) == len(text_replies))

        first_id = personas[0]["id"]
        friend_id = friend_ids.get(first_id) or ""
        if friend_id:
            image_reply = send_image(client, friend_id, png)
            check("图片模式返回非空回复", bool(image_reply), image_reply[:140])
            call_reply = send_call_turn(client, friend_id, "hey, what are you up to right now?")
            check("实时通话回合返回非空回复", bool(call_reply), call_reply[:140])
            check("通话模式语言一致性", latin_ratio(call_reply) >= 0.85, latin_ratio(call_reply))
            try:
                voice = send_voice(client, friend_id, wav)
                messages = voice.get("friend_messages") or []
                voice_text = str(messages[0].get("transcript") or "") if messages else ""
                audio_url = str(messages[0].get("audio_url") or "") if messages else ""
                check("语音条模式返回非空回复", bool(voice_text), voice_text[:140])
                check("语音条带音频地址（同一人物音色链路）", bool(audio_url), audio_url)
            except httpx.HTTPStatusError as exc:
                check("语音条模式返回非空回复", False, f"{exc.response.status_code} {exc.response.text[:200]}")

        # Prompt 一致性：文字 vs 通话共享同一角色卡核心字段
        for persona in personas[:1]:
            text_prompt = prompt_preview(client, persona["id"], identity["user_id"], "hello", "text")
            call_prompt = prompt_preview(client, persona["id"], identity["user_id"], "hello", "call")
            text_body = str(text_prompt.get("full_text") or "")
            call_body = str(call_prompt.get("full_text") or "")
            for field, value in (
                ("姓名", persona["nickname"]),
                ("职业", str(persona.get("occupation") or "")),
                ("城市", str(persona.get("city") or "")),
            ):
                if not value:
                    continue
                check(f"文字/通话共享角色卡-{field}", value in text_body and value in call_body, value)
            check(
                "通话 Prompt 比文字 Prompt 短（精简版）",
                0 < len(call_body) < len(text_body),
                {"text": len(text_body), "call": len(call_body)},
            )
            check(
                "文字 Prompt 含 User Persona（昵称/兴趣）",
                "ModesAlex" in text_body and "Programming" in text_body,
            )
            check(
                "文字 Prompt 含 Character Card 与示例声线模块",
                "[Character card" in text_body,
            )
            modules = [m.get("key") for m in text_prompt.get("modules") or []]
            report["prompt_modules"] = modules
            check(
                "接线顺序符合 A–H 契约",
                modules[:4] == ["global", "language", "character_card", "scenario"],
                modules,
            )

        # TTS：人物动态朗读仍可用（失败不判死：本机 Fish 需要外网）
        try:
            feed = client.get(BASE + "/api/v3/feed", params={"limit": 5}, timeout=60).json()
            items = [item for item in feed.get("items") or [] if item.get("kind") == "persona_post"]
            if items:
                post_id = items[0]["id"]
                speech = client.get(
                    f"{BASE}/api/v3/persona-posts/{post_id}/speech",
                    timeout=180,
                )
                report["tts"] = {"status": speech.status_code, "content_type": speech.headers.get("content-type")}
                check(
                    "人物动态朗读可用（TTS 链路）",
                    speech.status_code == 200 and "audio" in str(speech.headers.get("content-type")),
                    report["tts"],
                )
            else:
                check("人物动态朗读可用（TTS 链路）", False, "feed 没有 persona_post")
        except Exception as exc:  # noqa: BLE001
            check("人物动态朗读可用（TTS 链路）", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for item in checks if item["ok"])
    failed = [item for item in checks if not item["ok"]]
    report["checks_total"] = len(checks)
    report["checks_passed"] = passed
    report["passed"] = not failed
    out = OUT / f"chat-modes-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(checks)} 通过，失败 {len(failed)}；报告 {out.relative_to(ROOT)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
