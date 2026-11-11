"""V3 · 统一标签体系（稳定 ID + 多语言显示名 + 别名迁移）。

为什么需要
----------
升级前兴趣标签是**自由文本**：用户写「咖啡」，人物种子写 ``coffee``，帖子标签又是
``cafe``——推荐匹配因此漏掉大量本应命中的人。方案要求「标签统一为稳定 ID 与多语言
显示名称，迁移常用中英文别名，保留未知自定义标签；用户、人物、动态及广告使用同一套分类」。

约定
----
- **稳定 ID**：小写英文 slug（``coffee``、``cycling``），写入数据库的永远是它；
- **显示名**：按语言取 :data:`TAGS` 里的 ``labels``；没有翻译时退回 ID；
- **别名**：中英文常见写法都映射到同一 ID（``咖啡`` / ``coffee`` / ``espresso`` → ``coffee``）；
- **未知自定义标签**：保留为 ``custom:<slug>``，原文可在 :func:`custom_label` 还原，
  不会被静默丢弃；
- :func:`migrate_tags` 幂等，把历史资料/人物/动态里的自由文本统一成 ID。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 单个标签最长字符数（含 custom 前缀）
MAX_TAG_LEN = 40
#: 用户/动态标签数量上限
MAX_TAGS = 8
#: 未知自定义标签前缀
CUSTOM_PREFIX = "custom:"

#: 稳定 ID → 多语言显示名（zh 为主要面向用户的语言，en 兜底）
TAGS: dict[str, dict[str, str]] = {
    "coffee": {"zh": "咖啡", "en": "Coffee"},
    "cycling": {"zh": "骑行", "en": "Cycling"},
    "music": {"zh": "音乐", "en": "Music"},
    "photography": {"zh": "摄影", "en": "Photography"},
    "travel": {"zh": "旅行", "en": "Travel"},
    "reading": {"zh": "阅读", "en": "Reading"},
    "gaming": {"zh": "游戏", "en": "Gaming"},
    "cooking": {"zh": "烹饪", "en": "Cooking"},
    "movies": {"zh": "电影", "en": "Movies"},
    "hiking": {"zh": "徒步", "en": "Hiking"},
    "art": {"zh": "艺术", "en": "Art"},
    "design": {"zh": "设计", "en": "Design"},
    "coding": {"zh": "编程", "en": "Coding"},
    "running": {"zh": "跑步", "en": "Running"},
    "yoga": {"zh": "瑜伽", "en": "Yoga"},
    "cats": {"zh": "猫", "en": "Cats"},
    "dogs": {"zh": "狗", "en": "Dogs"},
    "food": {"zh": "美食", "en": "Food"},
    "fashion": {"zh": "时尚", "en": "Fashion"},
    "football": {"zh": "足球", "en": "Football"},
    "basketball": {"zh": "篮球", "en": "Basketball"},
    "swimming": {"zh": "游泳", "en": "Swimming"},
    "languages": {"zh": "语言学习", "en": "Languages"},
    "writing": {"zh": "写作", "en": "Writing"},
    "lifestyle": {"zh": "生活方式", "en": "Lifestyle"},
    "nature": {"zh": "自然", "en": "Nature"},
    "city": {"zh": "城市", "en": "City life"},
    "work": {"zh": "工作", "en": "Work"},
}

#: 别名 → 稳定 ID（全部小写；中英文混排）
ALIASES: dict[str, str] = {
    # coffee
    "咖啡": "coffee", "咖啡馆": "coffee", "espresso": "coffee", "latte": "coffee",
    "cafe": "coffee", "coffee shop": "coffee", "手冲": "coffee",
    # cycling
    "骑行": "cycling", "自行车": "cycling", "bike": "cycling", "bicycle": "cycling",
    "biking": "cycling", "单车": "cycling",
    # music
    "音乐": "music", "听歌": "music", "guitar": "music", "吉他": "music", "乐队": "music",
    "band": "music", "concert": "music", "live music": "music",
    # photography
    "摄影": "photography", "拍照": "photography", "photo": "photography",
    "photos": "photography", "camera": "photography", "相机": "photography",
    # travel
    "旅行": "travel", "旅游": "travel", "traveling": "travel", "travelling": "travel",
    "backpacking": "travel", "road trip": "travel",
    # reading
    "阅读": "reading", "读书": "reading", "books": "reading", "book": "reading",
    "novel": "reading", "小说": "reading",
    # gaming
    "游戏": "gaming", "打游戏": "gaming", "games": "gaming", "video games": "gaming",
    "board games": "gaming", "桌游": "gaming",
    # cooking
    "烹饪": "cooking", "做饭": "cooking", "cook": "cooking", "baking": "cooking",
    "烘焙": "cooking", "foodie": "cooking",
    # movies
    "电影": "movies", "movie": "movies", "film": "movies", "films": "movies",
    "cinema": "movies", "影院": "movies",
    # hiking
    "徒步": "hiking", "爬山": "hiking", "hike": "hiking", "trekking": "hiking",
    "mountain": "hiking", "户外": "hiking", "outdoors": "hiking",
    # art
    "艺术": "art", "画画": "art", "painting": "art", "drawing": "art", "museum": "art",
    "博物馆": "art",
    # design
    "设计": "design", "ui": "design", "ux": "design", "graphic design": "design",
    # coding
    "编程": "coding", "代码": "coding", "programming": "coding", "code": "coding",
    "developer": "coding", "技术": "coding", "tech": "coding",
    # running
    "跑步": "running", "run": "running", "marathon": "running", "马拉松": "running",
    "jogging": "running",
    # yoga
    "瑜伽": "yoga", "meditation": "yoga", "冥想": "yoga", "pilates": "yoga",
    # cats
    "猫": "cats", "猫咪": "cats", "cat": "cats", "kitten": "cats",
    # dogs
    "狗": "dogs", "狗狗": "dogs", "dog": "dogs", "puppy": "dogs", "宠物": "dogs",
    # food
    "美食": "food", "吃": "food", "foodie life": "food", "restaurant": "food",
    "餐厅": "food", "street food": "food",
    # fashion
    "时尚": "fashion", "穿搭": "fashion", "style": "fashion", "clothes": "fashion",
    "outfit": "fashion",
    # football
    "足球": "football", "soccer": "football", "football": "football", "世界杯": "football",
    # basketball
    "篮球": "basketball", "basketball": "basketball", "nba": "basketball",
    # swimming
    "游泳": "swimming", "swim": "swimming", "surfing": "swimming", "冲浪": "swimming",
    # languages
    "语言": "languages", "language": "languages", "英语": "languages", "english": "languages",
    "语言学习": "languages", "language learning": "languages", "学习语言": "languages",
    # writing
    "写作": "writing", "write": "writing", "writing": "writing", "journal": "writing",
    "日记": "writing",
    # lifestyle
    "生活": "lifestyle", "生活方式": "lifestyle", "lifestyle": "lifestyle", "日常": "lifestyle",
    # nature
    "自然": "nature", "nature": "nature", "植物": "nature", "plants": "nature",
    "花园": "nature", "garden": "nature",
    # city
    "城市": "city", "city": "city", "城市生活": "city", "city life": "city", "urban": "city",
    # work
    "工作": "work", "work": "work", "career": "work", "职场": "work", "创业": "work",
    "startup": "work",
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,31}$")


def _norm(text: Any) -> str:
    value = str(text or "").strip().lstrip("#").strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value[:MAX_TAG_LEN]


def _slug(text: str) -> str:
    ascii_text = re.sub(r"[^a-z0-9.\-]+", "-", text)
    return re.sub(r"-{2,}", "-", ascii_text).strip("-")[:32]


def _custom_id(text: str) -> str:
    """未知标签 → 稳定可读 ID：能 slug 就用 slug，中文等非 ASCII 用短哈希。"""
    slug = _slug(text)
    if slug and _SLUG_RE.match(slug):
        return CUSTOM_PREFIX + slug
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{CUSTOM_PREFIX}x-{digest}"


def canonical_tag(text: Any) -> str:
    """任意写法的标签 → 稳定 ID（未知返回 ``custom:...``，空返回 ""）。"""
    value = _norm(text)
    if not value:
        return ""
    if value in TAGS:
        return value
    alias = ALIASES.get(value)
    if alias:
        return alias
    # 去空格再试一次（"coffee shop" / "coffeeshop"）
    compact = value.replace(" ", "")
    if compact in ALIASES:
        return ALIASES[compact]
    # 「咖啡 / 摄影」这类用 / 或 , 拼在一起的取第一段
    for part in re.split(r"[/,，;；|、]", value):
        part = _norm(part)
        if part and part in ALIASES:
            return ALIASES[part]
        if part and part in TAGS:
            return part
    if value.startswith(CUSTOM_PREFIX):
        return value[:MAX_TAG_LEN]
    return _custom_id(value)


def canonical_tags(values: Iterable[Any] | None, *, limit: int = MAX_TAGS) -> list[str]:
    """列表清洗：canonical + 去重 + 保序 + 截断。"""
    out: list[str] = []
    for raw in values or []:
        tag = canonical_tag(raw)
        if tag and tag not in out:
            out.append(tag[:MAX_TAG_LEN])
        if len(out) >= max(1, int(limit)):
            break
    return out


def label(tag_id: str, lang: str = "zh") -> str:
    """显示名：已知 ID 取对应语言，未知自定义标签退回可读文本。"""
    key = str(tag_id or "").strip()
    if not key:
        return ""
    meta = TAGS.get(key)
    if meta:
        return meta.get(lang) or meta.get("en") or key
    if key.startswith(CUSTOM_PREFIX):
        return custom_label(key)
    return key


def custom_label(tag_id: str) -> str:
    """``custom:x-<hash>`` 无法还原原文时给一个可读占位，其余返回 slug。"""
    raw = str(tag_id or "")[len(CUSTOM_PREFIX):]
    if raw.startswith("x-"):
        return "自定义标签"
    return raw.replace("-", " ").strip() or "自定义标签"


def labels_for(tag_ids: Iterable[str] | None, lang: str = "zh") -> list[str]:
    return [label(item, lang) for item in (tag_ids or []) if str(item or "").strip()]


def labels_map(tag_ids: Iterable[str] | None, *, langs: Iterable[str] = ("zh", "en")) -> dict[str, dict[str, str]]:
    """给前端一次性下发 ID → 各语言显示名，避免前端自己维护映射。"""
    result: dict[str, dict[str, str]] = {}
    for tag_id in tag_ids or []:
        key = str(tag_id or "").strip()
        if not key or key in result:
            continue
        result[key] = {
            "zh": label(key, "zh"),
            "en": label(key, "en"),
            "known": key in TAGS,
        }
    return result


def is_known(tag_id: str) -> bool:
    return str(tag_id or "").strip() in TAGS


# --------------------------------------------------------------------------- #
# 历史数据迁移
# --------------------------------------------------------------------------- #
def _loads(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(str(raw)) if str(raw or "").strip() else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def migrate_tags(db: Any) -> dict:
    """把资料/人物/动态里的自由文本标签统一成稳定 ID（幂等）。

    - ``user_profiles.fact_sheet['interests']``
    - ``personas.interests``
    - ``persona_posts.tags`` / ``user_posts.tags``
    """
    if db is None:
        return {"profiles": 0, "personas": 0, "persona_posts": 0, "user_posts": 0}
    changed = {"profiles": 0, "personas": 0, "persona_posts": 0, "user_posts": 0}
    with db._lock, db.conn:  # noqa: SLF001
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(user_profiles)")}
        if "fact_sheet" in cols:
            rows = db.conn.execute(
                "SELECT user_id, fact_sheet FROM user_profiles WHERE COALESCE(fact_sheet,'') NOT IN ('', '{}')"
            ).fetchall()
            for row in rows:
                fact = _loads(row["fact_sheet"], {})
                interests = fact.get("interests") if isinstance(fact, dict) else None
                if not isinstance(interests, list) or not interests:
                    continue
                migrated = canonical_tags(interests)
                if migrated != [str(x) for x in interests]:
                    fact["interests"] = migrated
                    db.conn.execute(
                        "UPDATE user_profiles SET fact_sheet=? WHERE user_id=?",
                        (_dump(fact), row["user_id"]),
                    )
                    changed["profiles"] += 1

        rows = db.conn.execute(
            "SELECT id, interests FROM personas WHERE COALESCE(interests,'[]') NOT IN ('', '[]')"
        ).fetchall()
        for row in rows:
            data = _loads(row["interests"], [])
            if not isinstance(data, list) or not data:
                continue
            migrated = canonical_tags(data)
            if migrated != [str(x) for x in data]:
                db.conn.execute(
                    "UPDATE personas SET interests=? WHERE id=?", (_dump(migrated), row["id"])
                )
                changed["personas"] += 1

        for table, key in (("persona_posts", "persona_posts"), ("user_posts", "user_posts")):
            rows = db.conn.execute(
                f"SELECT id, tags FROM {table} WHERE COALESCE(tags,'[]') NOT IN ('', '[]')"
            ).fetchall()
            for row in rows:
                data = _loads(row["tags"], [])
                if not isinstance(data, list) or not data:
                    continue
                migrated = canonical_tags(data, limit=MAX_TAGS * 2)
                if migrated != [str(x) for x in data]:
                    db.conn.execute(
                        f"UPDATE {table} SET tags=? WHERE id=?", (_dump(migrated), row["id"])
                    )
                    changed[key] += 1
    if any(changed.values()):
        log.info("标签迁移：%s", changed)
    return changed


__all__ = [
    "ALIASES",
    "CUSTOM_PREFIX",
    "MAX_TAGS",
    "TAGS",
    "canonical_tag",
    "canonical_tags",
    "custom_label",
    "is_known",
    "label",
    "labels_for",
    "labels_map",
    "migrate_tags",
]
