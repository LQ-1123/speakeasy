"""每天给游客人物生成「生活图文 + 纯文字」动态（人设强约束）。

- 图：按兴趣/职业从 Openverse / Wikimedia 检索静物与场景，硬过滤掉人物图与
  女性服饰（男性人设），下载压缩到 data/media，登记 media_library 授权信息。
- 文案：LLM 读人物事实卡 + 图片事实卡后，用人物母语写「我的生活」，不得宣称
  照片里是自己、不得编造地点/日期/人物；失败用语言模板兜底。
- 纯文字：同一批人物额外发一条无图动态，保持信息流不是清一色照片。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx

from app.logging_utils import get_logger
from app.v3.daily_world import iso, now_local
from app.v3.media import (
    MediaItem,
    OpenverseAdapter,
    WikimediaAdapter,
    credit_text,
    download_image,
    is_life_safe,
    queries_for,
    relevant_to_query,
)

log = get_logger(__name__)

_IMAGE_PROMPT = """You are a normal person posting a photo from your daily life.

Persona (stay strictly in character):
{persona}

The photo (facts; you may ONLY reference these, nothing else):
- title: {title}
- tags: {tags}

Rules (must follow):
1. Write in MY native language ({language}). 1-2 short sentences, <= 180 characters.
2. Do NOT say or imply the photo is me, my face, or a selfie. Talk about the object/view/moment.
3. Do NOT name ANY city, country, landmark or neighbourhood. Describe it generically
   ("this street", "the table", "the window") — the photo may be from somewhere I have
   never been, so naming a place would break my story.
4. Do NOT invent dates, people, numbers, or events that are not in the photo facts.
5. No hashtags, no emoji, no mention of being an AI.
6. Return JSON only: {{"content": "...", "alt": "short English description for accessibility", "tags": ["..."]}}
"""

_TEXT_PROMPT = """You are a normal person posting a short text-only update.

Persona:
{persona}

Write ONE everyday status update in {language}, <= 140 characters, first person,
about something small: weather, work, food, mood, a plan, a tiny annoyance.
No hashtags, no emoji, no mention of being an AI. Do not repeat common clichés.
Return JSON only: {{"content": "...", "tags": ["..."]}}
"""

_FALLBACK_LIFE: dict[str, list[str]] = {
    "en": ["Quiet light like this is the whole reason I like early mornings.",
           "A small corner of the day that's just mine. Good enough."],
    "de": ["So ein ruhiges Licht ist der Grund, warum ich frühe Morgen mag.",
           "Eine kleine Ecke des Tages, die nur mir gehört. Reicht mir."],
    "es": ["Esta luz tranquila es la razón por la que me gustan las mañanas.",
           "Un rincón del día que es solo mío. Suficiente."],
    "it": ["Una luce così tranquilla è il motivo per cui amo le mattine.",
           "Un angolo della giornata tutto mio. Mi basta."],
    "pt": ["Uma luz assim, calma, é o motivo de eu gostar das manhãs.",
           "Um cantinho do dia que é só meu. Já me basta."],
    "ja": ["静かな光が好きだから、朝が好きなんだと思う。",
           "一日のうちの、自分だけの小さな隅。それで十分。"],
    "ko": ["조용한 빛을 좋아해서 아침이 좋은 것 같다.",
           "하루 중 나만의 작은 구석. 그거면 충분하다."],
    "zh": ["喜欢这种安静的光，所以早晨值得早起。",
           "一天里属于自己的小角落，够了。"],
}
_FALLBACK_TEXT: dict[str, list[str]] = {
    "en": ["Today's goal: one thing done well, everything else gently ignored.",
           "Rain again. I'm pretending that's a lifestyle choice."],
    "de": ["Ziel für heute: eine Sache gut machen, den Rest freundlich ignorieren.",
           "Schon wieder Regen. Ich nenne das jetzt Lebensstil."],
    "es": ["Objetivo de hoy: una cosa bien hecha y el resto ignorado con cariño.",
           "Otra vez llueve. Voy a llamarlo decisión de vida."],
    "it": ["Obiettivo di oggi: una cosa fatta bene, il resto ignorato con grazia.",
           "Di nuovo pioggia. Faccio finta che sia una scelta di stile."],
    "pt": ["Meta de hoje: uma coisa bem feita, o resto ignorado com carinho.",
           "Chuva outra vez. Vou fingir que é um estilo de vida."],
    "ja": ["今日の目標：ひとつだけ丁寧に。あとは静かに流す。",
           "また雨。これはもう生活様式ということにする。"],
    "ko": ["오늘 목표: 하나만 제대로. 나머지는 조용히 넘기기.",
           "또 비다. 이건 이제 라이프스타일이라고 하자."],
    "zh": ["今天的计划：把一件事做好，其余温柔地放过。",
           "又下雨了。我决定把它当作一种生活方式。"],
}


def _persona_facts(persona: dict) -> dict:
    return {
        "nickname": persona.get("nickname"),
        "gender": persona.get("gender"),
        "age": persona.get("age"),
        "country": persona.get("country"),
        "city": persona.get("city"),
        "occupation": persona.get("occupation"),
        "bio": persona.get("bio"),
        "personality": persona.get("personality"),
        "interests": persona.get("interests"),
        "language": persona.get("language"),
    }


PLACE_WORDS = {
    "new york", "nyc", "london", "paris", "berlin", "tokyo", "kyoto", "osaka", "seoul",
    "lisbon", "porto", "madrid", "barcelona", "rome", "milan", "venice", "florence",
    "berlin", "munich", "hamburg", "vienna", "prague", "amsterdam", "brussels", "copenhagen",
    "stockholm", "oslo", "helsinki", "dublin", "edinburgh", "manchester", "boston",
    "chicago", "san francisco", "los angeles", "seattle", "toronto", "vancouver",
    "mexico city", "são paulo", "sao paulo", "rio de janeiro", "buenos aires", "santiago",
    "bogotá", "bogota", "lima", "mumbai", "delhi", "bangalore", "bengaluru", "chennai",
    "singapore", "bangkok", "hanoi", "jakarta", "manila", "taipei", "hong kong",
    "shanghai", "beijing", "shenzhen", "guangzhou", "sydney", "melbourne", "auckland",
    "cairo", "cape town", "nairobi", "lagos", "istanbul", "athens", "warsaw", "budapest",
    "zurich", "geneva", "reykjavik", "dubai", "doha", "tel aviv", "moscow",
    "united states", "usa", "america", "england", "britain", "scotland", "ireland",
    "germany", "france", "italy", "spain", "portugal", "netherlands", "belgium",
    "denmark", "sweden", "norway", "finland", "poland", "czechia", "austria", "greece",
    "turkey", "russia", "india", "china", "japan", "korea", "brazil", "mexico",
    "argentina", "chile", "peru", "colombia", "canada", "australia", "new zealand",
    "egypt", "nigeria", "kenya", "south africa", "morocco", "thailand", "vietnam",
    "indonesia", "philippines", "malaysia", "taiwan",
}


def has_place_name(text: str) -> bool:
    """文案里出现具体地名 → 视为人设/事实违规（图片未必在人物所在城市）。"""
    lowered = (text or "").lower()
    return any(place in lowered for place in PLACE_WORDS)


def _fallback(pool: dict[str, list[str]], language: str, seed: str) -> str:
    options = pool.get(language) or pool["en"]
    index = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(options)
    return options[index]


async def _llm_json(llm: Any, messages: list[dict], max_tokens: int = 400) -> dict:
    if llm is None or getattr(llm, "uses_mock", False):
        return {}
    try:
        from app.v2.llm_json import complete_json_safe  # noqa: PLC0415

        data = await complete_json_safe(
            llm, messages, schema_hint='{"content": ""}', fallback=lambda: {},
            temperature=0.9, max_tokens=max_tokens,
        )
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.debug("生活动态生成失败: %s", exc)
        return {}


async def _pick_image(
    client: httpx.AsyncClient,
    adapters: list[Any],
    persona: dict,
    db: Any,
    used: set[str],
) -> MediaItem | None:
    for query in queries_for(persona):
        for adapter in adapters:
            results = await adapter.search(client, query, limit=8)
            for item in results:
                if item.external_id in used:
                    continue
                if not is_life_safe(item, persona):
                    continue
                if not relevant_to_query(item, query):
                    continue
                if _recently_used(db, item.external_id):
                    continue
                return item
    return None


def _recently_used(db: Any, external_id: str) -> bool:
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT external_id FROM media_library WHERE external_id=?", (external_id,)
        ).fetchone()
    return row is not None


def _record_media(db: Any, item: MediaItem, path: str, alt: str) -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO media_library (external_id, source, path, title, tags, license, author,"
            " source_url, fetched_at, used_count) VALUES (?,?,?,?,?,?,?,?,?,1)"
            " ON CONFLICT(external_id) DO UPDATE SET used_count=used_count+1",
            (
                item.external_id, item.source, path, item.title,
                json.dumps(item.tags, ensure_ascii=False),
                item.license, item.author, item.page_url, iso(now_local()),
            ),
        )


def _insert_post(
    db: Any,
    *,
    persona_id: str,
    content: str,
    language: str,
    expires_at: str,
    media: list[str] | None = None,
    alt: str = "",
    tags: list[str] | None = None,
    source_ref: dict | None = None,
    kind: str = "life_text",
    minutes_ago: int = 20,
) -> None:
    created = now_local().timestamp() - minutes_ago * 60
    from datetime import datetime, timezone  # noqa: PLC0415

    created_iso = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, media, tags, language, visibility,"
            " like_count, comment_count, created_at, expires_at, post_kind, alt_text, source_ref)"
            " VALUES (?,?,?,?,?,?, 'public', 0, 0, ?, ?, ?, ?, ?)",
            (
                f"pp_{uuid.uuid4().hex[:12]}",
                persona_id,
                content[:400],
                json.dumps(media or [], ensure_ascii=False),
                json.dumps(tags or [], ensure_ascii=False),
                language,
                created_iso,
                expires_at,
                kind,
                alt[:300],
                json.dumps(source_ref or {}, ensure_ascii=False),
            ),
        )


async def generate_life_posts(
    db: Any,
    llm: Any,
    personas: list[dict],
    *,
    media_dir: str,
    expires_at: str,
) -> dict:
    """给每位人物生成 1 条生活图文 + 1 条纯文字。返回统计。"""
    import os  # noqa: PLC0415

    if os.getenv("SE_MEDIA_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("图库已禁用（SE_MEDIA_ENABLED=0），跳过生活图文")
        return {"image_posts": 0, "text_posts": 0, "skipped_images": 0}

    # Wikimedia 元数据通常更准确（标题/描述可靠），优先；Openverse 兜底更多样
    adapters = [WikimediaAdapter(), OpenverseAdapter()]
    used: set[str] = set()
    images = 0
    texts = 0
    skipped = 0

    async with httpx.AsyncClient(timeout=35.0, follow_redirects=True) as client:
        for persona in personas:
            language = str(persona.get("language") or "en").strip().lower()
            if language not in _FALLBACK_LIFE:
                language = "en"
            facts = _persona_facts(persona)
            persona_block = json.dumps(facts, ensure_ascii=False)

            item = await _pick_image(client, adapters, persona, db, used)
            if item is not None:
                path = await download_image(client, item, media_dir)
                if path:
                    used.add(item.external_id)
                    data = await _llm_json(
                        llm,
                        [{"role": "user", "content": _IMAGE_PROMPT.format(
                            persona=persona_block, title=item.title or "(none)",
                            tags=", ".join(item.tags[:8]) or "(none)", language=language,
                        )}],
                    )
                    content = str(data.get("content") or "").strip()
                    if not content or has_place_name(content):
                        content = _fallback(_FALLBACK_LIFE, language, persona.get("id", ""))
                        skipped += 1
                    alt = str(data.get("alt") or "").strip() or item.title or "daily life photo"
                    tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()][:3]
                    _record_media(db, item, path, alt)
                    _insert_post(
                        db,
                        persona_id=str(persona["id"]),
                        content=content,
                        language=language,
                        expires_at=expires_at,
                        media=[path],
                        alt=alt,
                        tags=tags,
                        source_ref={
                            "kind": "stock_photo",
                            "credit": credit_text(item),
                            "license": item.license,
                            "url": item.page_url,
                        },
                        kind="life_photo",
                        minutes_ago=25,
                    )
                    images += 1
            else:
                skipped += 1

            # 纯文字动态
            text_data = await _llm_json(
                llm,
                [{"role": "user", "content": _TEXT_PROMPT.format(
                    persona=persona_block, language=language,
                )}],
                max_tokens=220,
            )
            text = str(text_data.get("content") or "").strip()
            if not text:
                text = _fallback(_FALLBACK_TEXT, language, persona.get("id", "") + ":text")
            _insert_post(
                db,
                persona_id=str(persona["id"]),
                content=text,
                language=language,
                expires_at=expires_at,
                tags=[str(t) for t in (text_data.get("tags") or [])][:3],
                kind="life_text",
                minutes_ago=90,
            )
            texts += 1

    log.info("生活动态：图文 %d、纯文字 %d、跳过图片 %d", images, texts, skipped)
    return {"image_posts": images, "text_posts": texts, "skipped_images": skipped}


__all__ = ["generate_life_posts"]
