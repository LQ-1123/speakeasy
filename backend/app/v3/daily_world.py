"""每日世界：每天 02:00 批量生成「N 个新人 + M 条动态」，24h 生命周期。

为什么这样做
------------
信息流不能永远只有一份静态语料。每天凌晨生成一批「今日世界」：
- 人物是**游客**（lifecycle=guest），24h 后如果没有任何互动就整批回收；
- 动态 24h 内可见；被互动过（赞/评论/加好友/浏览主页/朗读）的动态保留 7 天；
- 默认规模很小（5 人 × 6 条 = 30 条/天），可配；不够时惰性补量，避免空窗。

幂等：同一天只生成一次（``daily_world`` 表 day 主键）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, time, timedelta
from typing import Any

from app.logging_utils import get_logger
from app.v3.schema import backfill_avatar_seeds, sync_persona_friends
from app.v3.seed_data import LANGUAGE_NAMES, detect_language

log = get_logger(__name__)

TZ_NAME = os.getenv("SE_TZ", "Asia/Shanghai")
DAILY_HOUR = int(os.getenv("SE_DAILY_HOUR", "2"))
DAILY_PERSONAS = max(1, int(os.getenv("SE_DAILY_PERSONAS", "5")))
POSTS_PER_PERSONA = max(1, int(os.getenv("SE_DAILY_POSTS_PER_PERSONA", "3")))
POST_TTL_HOURS = max(1, int(os.getenv("SE_POST_TTL_HOURS", "24")))
POST_KEEP_DAYS = max(1, int(os.getenv("SE_POST_KEEP_DAYS", "7")))
LAZY_REFILL_BELOW = max(1, int(os.getenv("SE_FEED_MIN_POOL", "10")))

#: 语言 → 声线（与 world.py 保持一致）
VOICE_BY_LANG: dict[str, list[str]] = {
    "en": ["en-GB-RyanNeural", "en-US-AvaMultilingualNeural", "en-AU-WilliamMultilingualNeural"],
    "ja": ["ja-JP-KeitaNeural", "ja-JP-NanamiNeural"],
    "ko": ["ko-KR-InJoonNeural", "ko-KR-SunHiNeural"],
    "de": ["de-DE-FlorianMultilingualNeural", "de-DE-SeraphinaMultilingualNeural"],
    "es": ["es-ES-AlvaroNeural", "es-ES-XimenaNeural"],
    "it": ["it-IT-GiuseppeMultilingualNeural", "it-IT-IsabellaNeural"],
    "pt": ["pt-BR-AntonioNeural", "pt-BR-ThalitaMultilingualNeural"],
    "zh": ["zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural"],
}


# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #
def _tz():
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(TZ_NAME)
    except Exception:  # noqa: BLE001
        return None


def now_local() -> datetime:
    tz = _tz()
    return datetime.now(tz) if tz else datetime.now()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00") if dt.tzinfo is None else dt.astimezone(
        __import__("datetime").timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_iso(value: str) -> datetime | None:
    try:
        from datetime import timezone as _tzmod  # noqa: PLC0415

        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S+00:00").replace(tzinfo=_tzmod.utc)
    except (ValueError, TypeError):
        return None


def day_key(dt: datetime | None = None) -> str:
    return (dt or now_local()).date().isoformat()


def next_run_at(dt: datetime | None = None) -> datetime:
    """下一次每日生成时间（本地 02:00）。"""
    current = dt or now_local()
    target = datetime.combine(current.date(), time(hour=DAILY_HOUR), tzinfo=current.tzinfo)
    if target <= current:
        target = target + timedelta(days=1)
    return target


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
_TEMPLATE_PERSONAS: list[dict] = [
    {
        "nickname": "Nora", "gender": "female", "age": 27, "country": "Norway",
        "city": "Bergen", "language": "en", "occupation": "Marine biologist",
        "bio": "Marine biologist in Bergen. Cold water, warm coffee, very loud seagulls.",
        "personality": "Curious and dry-humoured, always halfway into a raincoat.",
        "interests": ["ocean", "hiking", "coffee", "photography"],
        "posts": [
            ("Saw a seal judge my kayak technique for a full minute. I deserved it.", ["ocean"]),
            ("Coffee outside in 4°C hits different when you're pretentious about it.", ["coffee"]),
            ("Fieldwork day: three samples, two soaked socks, one very happy scientist.", ["ocean"]),
            ("The mountain was there. I climbed it. The view was rude about it.", ["hiking"]),
            ("New rule: no meetings before I've seen the sea.", ["work"]),
            ("Found a tiny crab living in a bottle cap. Relocating him with full honours.", ["ocean"]),
        ],
    },
    {
        "nickname": "Tomas", "gender": "male", "age": 31, "country": "Czechia",
        "city": "Brno", "language": "en", "occupation": "Sound engineer",
        "bio": "Sound engineer in Brno. Old synths, new coffee, permanent headphone hair.",
        "personality": "Obsessive about details, gentle about people.",
        "interests": ["music", "engineering", "coffee", "cycling"],
        "posts": [
            ("Mixed the same snare for two hours and now I hear it in my sleep.", ["music"]),
            ("Fixed a 1983 synth with a paperclip and pure spite. Sounds incredible.", ["engineering"]),
            ("Cycled to the studio in the rain. The reverb was worth it.", ["cycling"]),
            ("Client said \"make it warmer\". I added 2% tape saturation. Magic.", ["music"]),
            ("Coffee count today: 4. Speaker count: 7. Regret count: 0.", ["coffee"]),
            ("Mixing at 2am is either genius or terrible. I'll report back.", ["music"]),
        ],
    },
    {
        "nickname": "Aiko", "gender": "female", "age": 24, "country": "Japan",
        "city": "Kyoto", "language": "ja", "occupation": "Ceramics student",
        "bio": "京都で陶芸を勉強中。土とお茶と、静かな朝。",
        "personality": "落ち着いていて、細かい手仕事の話になると急に饒舌になる。",
        "interests": ["ceramics", "tea", "photography", "walking"],
        "posts": [
            ("窯から出したばかりの碗が、思ったよりいい色になった。今日は勝ち。", ["ceramics"]),
            ("朝の鴨川を歩いた。人が少なくて、音がよく聞こえる。", ["walking"]),
            ("釉薬を変えたら全部失敗した。でも記録は残す。", ["ceramics"]),
            ("お茶を淹れる時間だけは、ちゃんと長くとることにしている。", ["tea"]),
            ("先生に「手が覚えている」と言われた。たぶん褒められている。", ["ceramics"]),
            ("古い町並みをフィルムで撮った。現像が楽しみ。", ["photography"]),
        ],
    },
    {
        "nickname": "Ravi", "gender": "male", "age": 29, "country": "India",
        "city": "Bengaluru", "language": "en", "occupation": "Backend engineer",
        "bio": "Backend engineer in Bengaluru. Filter coffee, badminton, too many tabs.",
        "personality": "Practical and funny, explains things with diagrams.",
        "interests": ["coding", "badminton", "coffee", "books"],
        "posts": [
            ("Production went down because of a typo in a config. I am the typo.", ["coding"]),
            ("Won a badminton match against the office champion. I will mention this forever.", ["badminton"]),
            ("Filter coffee at 4pm is a personality, not a habit.", ["coffee"]),
            ("Read a whole novel in one sitting. Back hurts. Worth it.", ["books"]),
            ("Rewrote a service to be 40% faster and now nobody trusts the graphs.", ["coding"]),
            ("Sunday plan: nothing. Aggressively nothing.", ["weekend"]),
        ],
    },
    {
        "nickname": "Sofia", "gender": "female", "age": 33, "country": "Portugal",
        "city": "Lisbon", "language": "pt", "occupation": "Cafe owner",
        "bio": "Dona de um café em Lisboa. Pastéis, vinis e conversas longas.",
        "personality": "Acolhedora e direta, gosta de música alta e de gente que fica até tarde.",
        "interests": ["coffee", "music", "food", "travel"],
        "posts": [
            ("Hoje o café encheu-se de estranhos que saíram amigos. Bom dia.", ["coffee"]),
            ("Pastéis de nata: 48 feitos, 3 sobreviveram à tarde.", ["food"]),
            ("Pus um disco de vinil antigo e o balcão inteiro parou para ouvir.", ["music"]),
            ("Choveu em Lisboa. Ninguém se importou. Nós também não.", ["travel"]),
            ("Cliente pediu \"o costume\". Não faço ideia de quem seja, mas acertei.", ["coffee"]),
            ("Fechar às 2h e ainda ficar a conversar na porta. É a vida.", ["music"]),
        ],
    },
]


_FALLBACK_LANG: dict[str, dict[str, Any]] = {
    "en": {
        "names": ["Emma", "Noah", "Chloe"],
        "city": "Bristol", "country": "United Kingdom", "occupation": "bookshop assistant",
        "bio": "Slow mornings, second-hand books and a kettle that never stops.",
        "posts": [
            "The kettle has been on three times and I have not made tea once. A productive morning.",
            "Someone put a tiny handwritten note inside a second-hand book today. I am keeping it.",
            "Rain all afternoon, so I rearranged the window display twice. It looks the same.",
        ],
    },
    "de": {
        "names": ["Lena", "Jonas", "Mia"],
        "city": "Leipzig", "country": "Germany", "occupation": "Grafikdesignerin",
        "bio": "Zeichnet morgens, kocht abends, und trinkt viel zu viel Kaffee.",
        "posts": [
            "Der Kaffee war heute so stark, dass ich die Wohnung zweimal aufgeräumt habe.",
            "Im Buchladen an der Ecke lag ein alter Reiseführer. Ich habe ihn nur angeschaut. Ehrlich.",
            "Regen den ganzen Nachmittag. Perfektes Wetter für einen langen Nachmittag mit Musik.",
        ],
    },
    "es": {
        "names": ["Lucía", "Mateo", "Paula"],
        "city": "Valencia", "country": "Spain", "occupation": "fotógrafa",
        "bio": "Café por la mañana, cámara al hombro, siesta cuando se puede.",
        "posts": [
            "Hoy el café sabía mejor que ayer. No sé si es el café o el día.",
            "Encontré una librería pequeña con sillas viejas. Me quedé una hora sin comprar nada.",
            "Ha llovido toda la tarde. He hecho tres fotos desde la ventana y ninguna me convence.",
        ],
    },
    "fr": {
        "names": ["Camille", "Hugo", "Léa"],
        "city": "Lyon", "country": "France", "occupation": "libraire",
        "bio": "Je lis le matin, je cuisine le soir, et je perds mes marque-pages.",
        "posts": [
            "Le café était trop chaud, alors j'ai lu deux pages de plus. Bon compromis.",
            "Quelqu'un a laissé un vieux billet dans un livre d'occasion. Je l'ai gardé.",
            "Il pleut depuis midi. J'ai réorganisé la vitrine deux fois, sans résultat.",
        ],
    },
    "it": {
        "names": ["Giulia", "Marco", "Sara"],
        "city": "Bologna", "country": "Italy", "occupation": "illustratrice",
        "bio": "Caffè, matite e una scrivania sempre piena di fogli.",
        "posts": [
            "Il caffè di oggi era così forte che ho riordinato la scrivania due volte.",
            "In libreria ho trovato una guida vecchia. L'ho solo sfogliata, giuro.",
            "Piove da stamattina. Perfetto per disegnare con la musica alta.",
        ],
    },
    "pt": {
        "names": ["Beatriz", "Tiago", "Inês"],
        "city": "Porto", "country": "Portugal", "occupation": "professora",
        "bio": "Café de manhã, livros à tarde, e uma varanda com vista para telhados.",
        "posts": [
            "O café hoje ficou tão forte que arrumei a casa duas vezes.",
            "Encontrei um livro antigo com uma dedicatória. Guardei como se fosse meu.",
            "Choveu toda a tarde. Fiquei na varanda a ver os telhados a brilhar.",
        ],
    },
    "ja": {
        "names": ["美咲", "悠斗", "彩"],
        "city": "金沢", "country": "Japan", "occupation": "編集者",
        "bio": "朝はコーヒー、夜は本。古い本屋を巡るのが趣味です。",
        "posts": [
            "今朝のコーヒーが濃すぎて、部屋を二回片付けました。",
            "古本屋で古い旅行ガイドを見つけました。買わずに一時間立ち読み。",
            "午後はずっと雨。音楽をかけて、窓の外を眺めていました。",
        ],
    },
    "ko": {
        "names": ["지우", "민준", "서연"],
        "city": "전주", "country": "Korea", "occupation": "편집자",
        "bio": "아침엔 커피, 저녁엔 책. 오래된 책방을 찾아다니는 게 취미예요.",
        "posts": [
            "오늘 커피가 너무 진해서 방을 두 번이나 정리했어요.",
            "동네 책방에서 오래된 여행 책을 봤어요. 그냥 한 시간 서서 읽었어요.",
            "오후 내내 비가 왔어요. 음악 틀고 창밖을 봤습니다.",
        ],
    },
    "zh": {
        "names": ["小雨", "阿哲", "林林"],
        "city": "杭州", "country": "China", "occupation": "编辑",
        "bio": "早上咖啡，晚上书，喜欢在老书店里慢慢逛。",
        "posts": [
            "今天的咖啡太浓了，我把房间收拾了两遍。",
            "在老书店翻到一本旧旅行指南，站着看了半小时，没买。",
            "下午一直在下雨，放了音乐，看着窗外出神。",
        ],
    },
}



# --------------------------------------------------------------------------- #
# 捏人素材池（data/lexicons/world.json，管理台可编辑）
# --------------------------------------------------------------------------- #
def _world_pools() -> dict:
    """读取捏人素材池；词库不可用时返回空 dict（走内置文案）。"""
    try:
        from app.v3 import lexicon  # noqa: PLC0415

        return lexicon.load("world") or {}
    except Exception:  # noqa: BLE001
        return {}


def _pool_items(pools: dict, category: str, language: str = "") -> list[str]:
    """取某类池子：优先该语言分组，其次 any，再次任意非空分组。"""
    group = pools.get(category) or {}
    if not isinstance(group, dict):
        return []
    if language and isinstance(group.get(language), list) and group[language]:
        return [str(x) for x in group[language] if str(x).strip()]
    if isinstance(group.get("any"), list) and group["any"]:
        return [str(x) for x in group["any"] if str(x).strip()]
    for items in group.values():
        if isinstance(items, list) and items:
            return [str(x) for x in items if str(x).strip()]
    return []


def _name_entries(pools: dict, language: str = "") -> list[dict]:
    """名字池归一化：兼容旧格式（纯字符串）与新格式（带性别/国家）。"""
    raw: list = []
    names = pools.get("names") or {}
    if not isinstance(names, dict):
        return []
    if language and isinstance(names.get(language), list) and names[language]:
        raw = list(names[language])
    elif isinstance(names.get("any"), list) and names["any"]:
        raw = list(names["any"])
    else:
        for items in names.values():
            if isinstance(items, list) and items:
                raw = list(items)
                break
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append({"name": item.strip(), "gender": "", "country": ""})
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                out.append(
                    {
                        "name": name,
                        "gender": str(item.get("gender") or "").strip().lower(),
                        "country": str(item.get("country") or "").strip(),
                    }
                )
    return out


def _city_for(pools: dict, country: str, index: int, day: str) -> str:
    """按名字标注的国家挑城市；该国没有城市池时退回 any / 第一个非空分组。"""
    cities = pools.get("cities") or {}
    if not isinstance(cities, dict):
        return ""
    group: list = []
    if country and isinstance(cities.get(country), list) and cities[country]:
        group = cities[country]
    elif isinstance(cities.get("any"), list) and cities["any"]:
        group = cities["any"]
    else:
        for items in cities.values():
            if isinstance(items, list) and items:
                group = items
                break
    items = [str(x) for x in group if str(x).strip()]
    return _pick_sequence(items, index, f"city:{day}")


def _pick_stable(items: list[str], index: int, salt: str = "") -> str:
    """确定性取一个：同一天同一索引永远取同一个（方便测试与复盘）。"""
    if not items:
        return ""
    digest = hashlib.sha256(f"{salt}:{index}".encode("utf-8")).hexdigest()[:8]
    return items[int(digest, 16) % len(items)]


def _pick_sequence(items: list[str], index: int, salt: str = "") -> str:
    """确定性顺序取：同一天内 index 不同就取不同项（池子够大时不重名）。"""
    if not items:
        return ""
    offset = int(hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8], 16) % len(items)
    return items[(offset + index) % len(items)]


def _pick_many(items: list[str], index: int, count: int, salt: str = "") -> list[str]:
    """确定性取多个（不重复相邻），用于兴趣标签。"""
    if not items:
        return []
    offset = int(hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8], 16) % len(items) if salt else 0
    picked: list[str] = []
    for step in range(count):
        value = items[(offset + index + step) % len(items)]
        if value not in picked:
            picked.append(value)
    return picked


def _pool_hint(pools: dict, language: str) -> str:
    """给 LLM 的素材提示：只给候选，不要求照抄（允许它自己衍生）。"""
    name_entries = _name_entries(pools, language)[:8]
    names = [
        f"{item['name']} ({item['gender']})" if item.get("gender") else item["name"]
        for item in name_entries
    ]
    occupations = _pool_items(pools, "occupations", language)[:10]
    cities = _pool_items(pools, "cities", language)[:8]
    interests = _pool_items(pools, "interests", language)[:14]
    lines: list[str] = []
    if names:
        lines.append(f"- Name ideas: {', '.join(names)}")
    if occupations:
        lines.append(f"- Occupation ideas: {', '.join(occupations)}")
    if cities:
        lines.append(f"- City ideas: {', '.join(cities)}")
    if interests:
        lines.append(f"- Interest tags to prefer: {', '.join(interests)}")
    if not lines:
        return ""
    return (
        "\n\nEditorial material (pick from these or invent close variants; "
        "keep the same style/level):\n" + "\n".join(lines)
    )


def _pick_edge_voice(db: Any, language: str, gender: str = "") -> str:
    """Choose a free catalog voice; never manufacture a shared default on exhaustion."""
    from app.voice.catalog import edge_voice_candidates, used_voice_ids

    lang = str(language or "en").strip().lower()
    candidates = list(edge_voice_candidates(lang, gender))
    if not candidates:
        return ""
    with db._lock:
        used = used_voice_ids(db, "voice_id")
    return next((voice for voice in candidates if voice not in used), "")


def _fallback_persona(
    index: int, day: str, language: str = "en", *, gender: str = ""
) -> dict:
    """分语言兜底人物：优先用管理台配置的素材池，池子为空时退回内置文案。

    ``gender``：强制性别（"female"/"male"）；为空时按 index 男女交替，保证新人物性别平衡。
    """
    lang = language if language in _FALLBACK_LANG else "en"
    spec = _FALLBACK_LANG[lang]
    pools = _world_pools()

    names = _pool_items(pools, "names", lang)
    occupations = _pool_items(pools, "occupations", lang)
    cities = _pool_items(pools, "cities", lang)  # 仅用于"有没有城市池"的判断
    interests = _pool_items(pools, "interests", lang)

    name_entries = _name_entries(pools, lang)
    target_gender = str(gender or "").strip().lower()
    if target_gender not in ("female", "male"):
        target_gender = "female" if index % 2 == 0 else "male"
    entry: dict = {}
    if name_entries:
        same_gender = [
            e for e in name_entries if str(e.get("gender") or "").lower() == target_gender
        ]
        pool = same_gender or name_entries
        entry = pool[
            (
                int(hashlib.sha256(f"name:{day}".encode()).hexdigest()[:8], 16)
                + index
            )
            % len(pool)
        ]
        name = entry["name"]
    else:
        suffix = hashlib.sha256(f"{day}:{index}".encode("utf-8")).hexdigest()[:4]
        name = f"{spec['names'][index % len(spec['names'])]} {suffix[:2]}"

    posts = [
        {"content": text, "tags": []} if isinstance(text, str) else text
        for text in spec["posts"]
    ]
    gender = entry.get("gender") or target_gender
    country = entry.get("country") or spec["country"]
    return {
        "nickname": name,
        "gender": gender,
        "age": 24 + (index * 3) % 12,
        "country": country,
        "city": _city_for(pools, country, index, day) or spec["city"],
        "language": lang,
        "occupation": _pick_sequence(occupations, index, f"job:{day}") or spec["occupation"],
        "bio": spec["bio"],
        "personality": "curious, calm, a little funny",
        "interests": _pick_many(interests, index, 4, f"interest:{day}")
        or ["coffee", "books", "walking"],
        "posts": posts,
    }



    base = _TEMPLATE_PERSONAS[index % len(_TEMPLATE_PERSONAS)]
    suffix = hashlib.sha256(f"{day}:{index}".encode("utf-8")).hexdigest()[:4]
    persona = dict(base)
    persona["nickname"] = f"{base['nickname']}"
    # 每天给同一模板加一点差异（避免完全重复）
    persona["bio"] = f"{base['bio']} · {suffix}"
    return persona


_PROMPT = """You are writing the daily cast of a language-learning social app.
Create ONE AI persona and 6 short posts in their voice.

Rules:
- All text (bio, personality, posts) must be in the persona's native language (`language`).
- The persona is a normal person abroad: job, city, small daily details, light humour.
- Posts: first person, <= 200 characters, no hashtags, no emoji, distinct topics.
- interests: 3-4 lowercase english tags.
- Do not mention being an AI.

Output JSON only:
{"nickname","gender":"male|female","age","country","city","language":"en|ja|ko|de|es|it|pt|zh",
 "occupation","bio","personality","interests":[],"posts":[{"content","tags":[]}]}
"""


LEVEL_GUIDE = {
    "刚开始": "absolute beginner (A1): very short simple sentences, common words only",
    "能聊几句": "elementary (A2-B1): short sentences, everyday vocabulary",
    "日常对话": "intermediate (B1-B2): natural daily conversation, some idioms",
    "比较流利": "advanced (C1): richer vocabulary, nuance and humour allowed",
}


def _user_lang_prefs(db: Any) -> tuple[list[str], dict[str, str]]:
    """读取用户在练语言与各自水平。"""
    try:
        # The world is shared. Build language coverage from registered users' own
        # rows; never borrow a local user's private fact sheet for another user.
        with db._lock:
            rows = db.conn.execute("SELECT target_languages,fact_sheet FROM user_profiles ORDER BY user_id").fetchall()
        if rows:
            targets: list[str] = []
            levels: dict[str, str] = {}
            for row in rows:
                for lang in json.loads(row["target_languages"] or "[]"):
                    if str(lang).strip() and str(lang) not in targets:
                        targets.append(str(lang))
                fact = json.loads(row["fact_sheet"] or "{}")
                for lang, level in ((fact.get("language_profile") or {}).get("levels") or {}).items():
                    levels.setdefault(str(lang), str(level))
            return targets, levels
        profile = db.get_profile() or {}
        targets = [str(x) for x in (profile.get("target_languages") or []) if str(x).strip()]
        fact = profile.get("fact_sheet") or {}
        if isinstance(fact, str):
            import json as _json  # noqa: PLC0415

            fact = _json.loads(fact)
        levels = ((fact.get("language_profile") or {}).get("levels") or {}) if isinstance(fact, dict) else {}
        return targets, {str(k): str(v) for k, v in levels.items()}
    except Exception:  # noqa: BLE001
        return [], {}


async def _llm_persona(
    llm: Any, index: int, day: str, *, language: str = "", level: str = ""
) -> dict | None:
    """生成一个人物：语言必须是在练语言，文案难度跟随该语言的水平。"""
    if llm is None or getattr(llm, "uses_mock", False):
        return None
    guide = LEVEL_GUIDE.get(level, LEVEL_GUIDE["能聊几句"]) if level else ""
    parts = [_PROMPT, f"\n\nCreate persona #{index + 1} for {day}."]
    hint = _pool_hint(_world_pools(), language)
    if hint:
        parts.append(hint)
    if language:
        parts.append(f"Write ALL text (bio, personality, posts) in language code '{language}'.")
    if level:
        parts.append(f"Reader level for {language}: {guide}.")
    from app.v2.llm_json import complete_json_safe  # noqa: PLC0415

    async def _try(extra: str = "") -> dict | None:
        msgs = " ".join(parts + ([extra] if extra else []))
        try:
            data = await complete_json_safe(
                llm,
                [
                    # 注意：这个 prompt 用 system 角色时 DeepSeek 会返回空串，必须放 user 消息
                    {"role": "user", "content": msgs},
                ],
                schema_hint='{"nickname","gender","age","country","city","language","occupation","posts":[]}',
                fallback=lambda: {},
                temperature=1.0,
                max_tokens=2000,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("每日人物生成失败 #%d: %s", index, exc)
            return None
        if not isinstance(data, dict) or not data.get("nickname"):
            return None
        if not isinstance(data.get("posts"), list) or not data["posts"]:
            return None
        return data

    data = await _try()
    if data is None:
        return None

    # 校验真实语言：角标必须和内容一致
    if language:
        texts = [str(p.get("content") or "") for p in data["posts"] if isinstance(p, dict)]
        detected = detect_language(" ".join(texts)) if texts else ""
        if detected and detected != language:
            log.info("人物语言不符（要求 %s，实际 %s），重试一次", language, detected)
            data = await _try(
                f"IMPORTANT: the previous attempt was written in '{detected}'. "
                f"You MUST write every post in '{language}' this time."
            )
            if data is None:
                return None
            texts = [str(p.get("content") or "") for p in data["posts"] if isinstance(p, dict)]
            detected = detect_language(" ".join(texts)) if texts else ""
            if detected and detected != language:
                log.warning("人物语言仍不符（要求 %s，实际 %s），改走兜底", language, detected)
                return None
        data["language"] = language
    return data


async def _pick_fish_voice(db: Any, language: str, gender: str) -> str:
    """从 Fish 音色库挑一个尽量没被别人用过的音色（按语言+性别）。

    Fish 不可达时**立即返回空串**（由调用方用 Edge 声线兜底）：
    否则每生成一个人物都要等 3 页音色列表 × 30s 超时，每日世界会卡住好几分钟。
    """
    try:
        from app.config import get_settings  # noqa: PLC0415
        from app.voice import fish_tts, voice_registry  # noqa: PLC0415
        from app.voice.tts_config import TTSConfigStore  # noqa: PLC0415

        cfg = TTSConfigStore(str(get_settings().data_path)).load()
        if str(getattr(cfg, "provider", "")) != "fish":
            return ""
        key = str(getattr(cfg, "fish_api_key", "") or "").strip()
        if not key:
            return ""
        if not voice_registry.fish_usable(cfg):
            return ""  # 熔断窗口内：不再尝试联网
        with db._lock:  # noqa: SLF001
            used = {
                str(r[0])
                for r in db.conn.execute(
                    "SELECT DISTINCT fish_voice_id FROM personas"
                    " WHERE COALESCE(fish_voice_id, '') != ''"
                ).fetchall()
            }
        base_url = str(getattr(cfg, "fish_base_url", "https://api.fish.audio") or "")
        # 管理台「音色池」里手动加入的音色优先（排除已被占用的）
        try:
            from app.admin import voice_pool  # noqa: PLC0415

            picked = voice_pool.pick_from_pool(language or "en", gender or "", used)
            if picked:
                return picked
        except Exception as exc:  # noqa: BLE001
            log.debug("从素材池挑音色失败（继续用 Fish 官方池）: %s", exc)
        # 先做一次 5 秒探测：不可达就熔断，后面的人物直接走 Edge 声线，
        # 否则每个新人物都要等一次网络超时，每日世界会卡住几分钟。
        try:
            await fish_tts.list_models(
                api_key=key, base_url=base_url, page_size=1, timeout=5.0
            )
        except Exception as exc:  # noqa: BLE001
            voice_registry.mark_fish_failure(exc)
            log.info("Fish 音色库不可达，本轮新人物改用 Edge 声线兜底")
            return ""
        plan = [
            (language or "en", gender or ""),
            (language or "en", gender or ""),
            ("", gender or ""),
            ("", gender or ""),
            ("", ""),
            ("", ""),
        ]
        candidate = ""
        for lang_arg, gender_arg in plan:
            candidate = await fish_tts.pick_voice_for(
                language=lang_arg,
                gender=gender_arg,
                api_key=key,
                base_url=base_url,
                timeout=5.0,  # 网络不可达时快速失败，别拖住每日世界
                pages=1,
            )
            if candidate and candidate not in used:
                return candidate
        # Exhaustion leaves this persona unassigned; reusing the last provider result
        # would make two active roles share one Fish identity.
        return ""
    except Exception as exc:  # noqa: BLE001
        try:
            from app.voice import voice_registry  # noqa: PLC0415

            voice_registry.mark_fish_failure(exc)
        except Exception:  # noqa: BLE001
            pass
        log.debug("分配 Fish 音色失败: %s", exc)
        return ""


async def _insert_persona(db: Any, persona: dict, *, day: str, expires_at: str) -> tuple[str, int]:
    """写入一位游客人物 + 它的动态，返回 (persona_id, 帖数)。"""
    from app.v3.avatar_style import default_options

    pid = f"p_{uuid.uuid4().hex[:10]}"
    lang = str(persona.get("language") or "en").strip().lower()
    if lang not in VOICE_BY_LANG:
        lang = "en"
    created = now_local()
    posts = list(persona.get("posts") or [])[:POSTS_PER_PERSONA]
    options = json.dumps(default_options({**persona, "id": pid}), ensure_ascii=False)
    fish_ref = await _pick_fish_voice(db, lang, str(persona.get("gender") or ""))
    strict = os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
    verified_edge = ""
    if strict:
        from app.voice.catalog import verify_voice

        if fish_ref and not await verify_voice(db, "fish", fish_ref, language=lang):
            fish_ref = ""
        verified_edge = _pick_edge_voice(db, lang, str(persona.get("gender") or ""))
        if verified_edge and not await verify_voice(db, "edge", verified_edge, language=lang):
            verified_edge = ""
    with db._lock, db.conn:  # noqa: SLF001
        if not db.conn.in_transaction:
            db.conn.execute("BEGIN IMMEDIATE")
        edge_voice = _pick_edge_voice(db, lang, str(persona.get("gender") or ""))
        if strict and edge_voice != verified_edge:
            edge_voice = ""
        if fish_ref and db.conn.execute(
            "SELECT 1 FROM personas WHERE status='active' AND fish_voice_id=?", (fish_ref,)
        ).fetchone():
            fish_ref = ""
        voice_ready = bool(edge_voice or (strict and fish_ref))
        db.conn.execute(
            "INSERT INTO personas (id, nickname, avatar, avatar_seed, avatar_style, avatar_options,"
            " bio, personality, interests, language, country, city, timezone, occupation, age,"
            " gender, voice_id, fish_voice_id, archetype_id, ai_config, status, created_at,"
            " lifecycle, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                str(persona.get("nickname") or "Guest")[:40],
                "",
                "persona-" + pid[2:],
                "avataaars",
                options,
                str(persona.get("bio") or "")[:400],
                str(persona.get("personality") or "")[:600],
                json.dumps(persona.get("interests") or [], ensure_ascii=False),
                lang,
                str(persona.get("country") or "")[:60],
                str(persona.get("city") or "")[:60],
                "UTC",
                str(persona.get("occupation") or "")[:80],
                int(persona.get("age") or 27),
                str(persona.get("gender") or "female"),
                edge_voice,
                fish_ref,
                "",
                json.dumps({"generated": "daily", "day": day}, ensure_ascii=False),
                "active" if voice_ready else "waiting_voice",
                iso(created),
                "guest",
                expires_at,
            ),
        )
        written = 0
        for idx, raw_post in enumerate(posts):
            if isinstance(raw_post, dict):
                content = str(raw_post.get("content") or "").strip()[:400]
                tags = raw_post.get("tags") or []
            elif isinstance(raw_post, (list, tuple)) and raw_post:
                content = str(raw_post[0] or "").strip()[:400]
                tags = raw_post[1] if len(raw_post) > 1 else []
            else:
                continue
            if not content:
                continue
            post_created = created - timedelta(minutes=30 + idx * 47 + (idx * idx) % 20)
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at, expires_at)"
                " VALUES (?,?,?, '[]', ?, ?, ?, 0, 0, ?, ?)",
                (
                    f"pp_{uuid.uuid4().hex[:12]}",
                    pid,
                    content,
                    json.dumps(tags or [], ensure_ascii=False),
                    lang,
                    "public" if voice_ready else "waiting_voice",
                    iso(post_created),
                    expires_at,
                ),
            )
            written += 1
    return pid, written


async def generate_daily(
    db: Any, llm: Any, *, day: str | None = None, force: bool = False,
    media_dir: str | None = None,
) -> dict:
    """生成「今日世界」：N 个游客人物 × M 条动态。同一天幂等。"""
    day = day or day_key()
    now = now_local()
    expires = iso(now + timedelta(hours=POST_TTL_HOURS))

    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM daily_world WHERE day=?", (day,)).fetchone()
    if row is not None and not force:
        return {
            "day": day, "skipped": True,
            "personas": int(row["personas"]), "posts": int(row["posts"]),
            "source": str(row["source"]),
        }

    created_personas = 0
    created_posts = 0
    source = "llm"
    persona_rows: list[dict] = []
    targets, levels = _user_lang_prefs(db)
    for index in range(DAILY_PERSONAS):
        lang = targets[index % len(targets)] if targets else "en"
        level = levels.get(lang, "能聊几句")
        persona = await _llm_persona(llm, index, day, language=lang, level=level)
        if persona is not None:
            persona["language"] = lang
        if persona is None:
            persona = _fallback_persona(index, day, lang)
            source = "template" if index == 0 else source
        pid, posts = await _insert_persona(db, persona, day=day, expires_at=expires)
        created_personas += 1
        created_posts += posts
        persona_rows.append({**persona, "id": pid})

    # 游客人物的表达层（Speech DNA + 12 组示例）：确定性生成，**0 次 LLM 调用**
    if persona_rows:
        try:
            from app.v3.speech_dna import ensure_persona_dna  # noqa: PLC0415

            dna_stats = await ensure_persona_dna(db, None, persona_rows, use_llm=False)
            log.info("每日人物表达层：%s", dna_stats)
        except Exception as exc:  # noqa: BLE001 - 表达层失败不影响信息流
            log.debug("每日人物表达层补齐失败（忽略）: %s", exc)

    # 生活图文 + 纯文字（图库零 key 源，人设硬过滤）
    life_stats = {"image_posts": 0, "text_posts": 0, "skipped_images": 0}
    if persona_rows:
        try:
            from app.v3.life_posts import generate_life_posts  # noqa: PLC0415

            media_root = media_dir or os.path.join(os.getenv("SE_DATA_DIR", "data"), "media")
            life_stats = await generate_life_posts(
                db, llm, persona_rows, media_dir=media_root, expires_at=expires
            )
            created_posts += life_stats["image_posts"] + life_stats["text_posts"]
        except Exception as exc:  # noqa: BLE001
            log.warning("生活图文生成失败（不影响文字动态）: %s", exc)

    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO daily_world (day, generated_at, personas, posts, source)"
            " VALUES (?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET generated_at=excluded.generated_at,"
            " personas=excluded.personas, posts=excluded.posts, source=excluded.source",
            (day, iso(now), created_personas, created_posts, source),
        )

    sync_persona_friends(db)
    backfill_avatar_seeds(db)
    log.info("每日世界 %s：%d 人物 / %d 动态（%s）", day, created_personas, created_posts, source)
    return {
        "day": day, "skipped": False, "personas": created_personas,
        "posts": created_posts, "source": source, "expires_at": expires,
        "life": life_stats,
    }


async def ensure_today(db: Any, llm: Any, *, media_dir: str | None = None) -> dict:
    """确保今天有内容：没有生成过就生成（惰性补量）。"""
    return await generate_daily(db, llm, media_dir=media_dir)


def daily_status(db: Any) -> dict:
    day = day_key()
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM daily_world WHERE day=?", (day,)).fetchone()
        visible = db.conn.execute(
            "SELECT COUNT(*) AS n FROM persona_posts"
            " WHERE visibility='public' AND (expires_at='' OR expires_at>?)",
            (iso(now_local()),),
        ).fetchone()["n"]
        guests = db.conn.execute(
            "SELECT COUNT(*) AS n FROM personas WHERE lifecycle='guest'"
        ).fetchone()["n"]
        media_count = db.conn.execute("SELECT COUNT(*) AS n FROM media_library").fetchone()["n"]
    return {
        "day": day,
        "generated": row is not None,
        "generated_at": str(row["generated_at"]) if row is not None else "",
        "today_personas": int(row["personas"]) if row is not None else 0,
        "today_posts": int(row["posts"]) if row is not None else 0,
        "source": str(row["source"]) if row is not None else "",
        "visible_posts": int(visible or 0),
        "guest_personas": int(guests or 0),
        "media_library": int(media_count or 0),
        "next_run_at": next_run_at().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": {
            "personas_per_day": DAILY_PERSONAS,
            "posts_per_persona": POSTS_PER_PERSONA,
            "hour": DAILY_HOUR,
            "post_ttl_hours": POST_TTL_HOURS,
            "post_keep_days": POST_KEEP_DAYS,
        },
    }


async def daily_loop(db: Any, llm: Any, *, tick_s: float = 30.0, media_dir: str | None = None) -> None:
    """后台循环：启动先补今天的内容，之后每天 02:00 生成；顺带跑 TTL 清理。"""
    from app.v3.lifecycle import cleanup  # noqa: PLC0415

    try:
        await ensure_today(db, llm, media_dir=media_dir)
        cleanup(db)
    except Exception as exc:  # noqa: BLE001
        log.warning("每日世界初始化失败: %s", exc)

    last_cleanup = 0.0
    while True:
        try:
            await asyncio.sleep(tick_s)
            import time as _time  # noqa: PLC0415

            now = now_local()
            status = daily_status(db)
            if not status["generated"] and now.hour >= DAILY_HOUR:
                await generate_daily(db, llm, media_dir=media_dir)
            elif status["visible_posts"] < LAZY_REFILL_BELOW:
                # 内容不够看：惰性补今天这一批（幂等，已生成则跳过）
                await ensure_today(db, llm, media_dir=media_dir)
            if _time.time() - last_cleanup > 600:
                cleanup(db)
                last_cleanup = _time.time()
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("每日世界循环异常: %s", exc)




def _total_entries(group: Any) -> int:
    """统计一个素材类别的总条数（列表或"分组 → 列表"都支持）。"""
    if isinstance(group, list):
        return len([x for x in group if str(x).strip()])
    if isinstance(group, dict):
        return sum(
            len([x for x in items if str(x).strip()])
            for items in group.values()
            if isinstance(items, list)
        )
    return 0


def preview_personas(count: int = 3, language: str = "en", day: str = "") -> list[dict]:
    """按当前素材池"试捏"几个人物（**不写库、不调 LLM**），用于管理台预览。

    走的是与兜底捏人完全相同的取池逻辑，所以池子改了，这里立刻能看出效果。
    """
    lang = str(language or "en").strip().lower() or "en"
    target = max(1, min(8, int(count or 3)))
    seed_day = day or day_key()
    pools = _world_pools()
    out: list[dict] = []
    for index in range(target):
        persona = _fallback_persona(index, f"{seed_day}:preview", lang)
        out.append(
            {
                "nickname": persona.get("nickname"),
                "gender": persona.get("gender"),
                "age": persona.get("age"),
                "country": persona.get("country"),
                "city": persona.get("city"),
                "occupation": persona.get("occupation"),
                "language": persona.get("language"),
                "interests": list(persona.get("interests") or []),
                "bio": persona.get("bio"),
            }
        )
    def _total(group: Any) -> int:
        if isinstance(group, list):
            return len([x for x in group if str(x).strip()])
        if isinstance(group, dict):
            return sum(
                len([x for x in items if str(x).strip()])
                for items in group.values()
                if isinstance(items, list)
            )
        return 0

    warnings: list[str] = []
    name_count = len(_name_entries(pools, lang))
    if 0 < name_count < target:
        warnings.append(f"名字池只有 {name_count} 个，预览会出现重复；建议补到 {target} 个以上")
    occupation_count = _total(pools.get("occupations"))
    if 0 < occupation_count < target:
        warnings.append(
            f"职业池只有 {occupation_count} 个，同一天可能出现重复职业；建议补到 {target} 个以上"
        )
    interest_count = _total(pools.get("interests"))
    if 0 < interest_count < 8:
        warnings.append(f"兴趣池只有 {interest_count} 个，人物标签会比较雷同；建议补到 8 个以上")
    return {
        "day": seed_day,
        "language": lang,
        "count": len(out),
        "warnings": warnings,
        "pools": {
            "names": len(_name_entries(pools, lang)),
            "occupations": _total_entries(pools.get("occupations")),
            "cities": _total_entries(pools.get("cities")),
            "interests": _total_entries(pools.get("interests")),
        },
        "personas": out,
    }


__all__ = [
    "DAILY_HOUR",
    "DAILY_PERSONAS",
    "POSTS_PER_PERSONA",
    "POST_KEEP_DAYS",
    "POST_TTL_HOURS",
    "daily_loop",
    "daily_status",
    "day_key",
    "ensure_today",
    "generate_daily",
    "next_run_at",
    "now_local",
    "preview_personas",
]
