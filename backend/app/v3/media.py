"""开放图库适配器（零 key）：Openverse / Wikimedia Commons。

只做「生活图」：按人物兴趣检索静物/场景，硬性过滤掉带人物的图，
避免「男性程序员发女性裙子」这类人设错配。图片落盘到 data/media 并登记授权信息。
"""

from __future__ import annotations

import hashlib
import html
import io
import os
import re
from dataclasses import dataclass, field

import httpx
from PIL import Image

from app.logging_utils import get_logger

log = get_logger(__name__)

USER_AGENT = (
    "SpeakEasyDemo/3.0 (https://github.com/LQ-1123/speakeasy; contact: maintainer) "
    "httpx/0.28"
)
ALLOWED_LICENSES = {"cc0", "pdm", "by", "by-sa", "public domain", "cc by", "cc by-sa"}

#: 出现这些词 → 图里明显有人，直接丢弃（不用真人照片冒充人设本人）
PERSON_WORDS = {
    "woman", "women", "man", "men", "girl", "girls", "boy", "boys", "person", "people",
    "portrait", "selfie", "face", "model", "child", "children", "kid", "baby", "bride",
    "groom", "couple", "family", "crowd", "human", "hands", "hand", "legs", "feet",
    "tattoo", "makeup", "smile", "smiling",
}
#: 女性化服饰/物品：男性人设直接排除（防人设崩）
FEMININE_WORDS = {
    "dress", "dresses", "skirt", "skirts", "heels", "high heels", "blouse", "lingerie",
    "bikini", "swimsuit", "handbag", "purse", "clutch", "gown", "wedding dress",
    "saree", "sari", "jumpsuit", "romper", "leggings", "tights",
}
SENSITIVE_WORDS = {
    "gun", "weapon", "blood", "nude", "naked", "sexy", "erotic", "war", "soldier",
    "protest", "police", "riot", "corpse", "dead body", "drug", "cigarette", "alcohol",
    "beer", "wine", "whiskey", "cocktail",
}

#: 兴趣 → 具体静物/场景检索词（避免抽象词拉回人像）
QUERY_BY_INTEREST: dict[str, list[str]] = {
    "coffee": ["espresso machine", "coffee cup table", "cafe interior empty"],
    "ocean": ["ocean waves rocks", "coastline cliff", "seagull sea"],
    "hiking": ["mountain trail", "hiking boots trail", "forest path fog"],
    "photography": ["film camera desk", "vintage camera", "camera lens closeup"],
    "music": ["vinyl record player", "synthesizer keyboard", "guitar corner room"],
    "coding": ["mechanical keyboard desk", "desk setup monitor", "code screen closeup"],
    "books": ["stack of books", "open book table", "bookshelf library"],
    "food": ["pastel de nata", "bread bakery", "fresh pasta kitchen"],
    "travel": ["lisbon tram street", "old town alley", "train window landscape"],
    "cycling": ["bicycle road", "bike leaning wall", "bicycle wheel closeup"],
    "badminton": ["badminton racket", "badminton court"],
    "ceramics": ["ceramic bowl handmade", "pottery wheel", "clay hands"],
    "tea": ["teapot cup", "matcha bowl", "tea ceremony"],
    "oceanography": ["research vessel", "microscope lab", "ocean sample jars"],
    "sailing": ["sailboat harbor", "sailing sea"],
    "literature": ["old book pages", "typewriter desk"],
    "design": ["sketchbook pencils", "color swatches design", "architecture model"],
    "architecture": ["modern building facade", "concrete stairs", "wooden house window"],
    "art": ["museum gallery room", "painting frame wall", "sculpture garden"],
    "gardening": ["plants window sill", "garden pots", "watering can plants"],
    "pubs": ["pub interior empty", "beer tap bar", "wooden pub table"],
    "football": ["football pitch", "stadium seats", "football boots"],
    "gaming": ["game controller desk", "arcade machine", "gaming setup"],
    "bossa nova": ["acoustic guitar room", "record sleeve music"],
    "cinema": ["cinema seats empty", "film reel", "movie theater screen"],
    "languages": ["notebook handwriting", "language dictionary"],
    "marketing": ["desk notebook laptop", "whiteboard sticky notes"],
    "engineering": ["circuit board closeup", "electronics workbench"],
}
FALLBACK_QUERIES = ["city street morning", "window plants light", "desk notebook coffee"]


@dataclass
class MediaItem:
    external_id: str
    source: str
    title: str
    tags: list[str] = field(default_factory=list)
    license: str = ""
    author: str = ""
    page_url: str = ""
    image_url: str = ""
    thumb_url: str = ""

    def text_blob(self) -> str:
        return " ".join([self.title or "", " ".join(self.tags or [])]).lower()


def _headers() -> dict[str, str]:
    """Wikimedia 机器人政策要求可识别的 UA + Api-User-Agent。"""
    return {"User-Agent": USER_AGENT, "Api-User-Agent": USER_AGENT}


async def _get_json(
    client: httpx.AsyncClient, url: str, params: dict, *, attempts: int = 3
) -> dict | None:
    """带退避重试的 JSON GET（Openverse 偶发 504）。"""
    import asyncio  # noqa: PLC0415

    for attempt in range(attempts):
        try:
            resp = await client.get(url, params=params, headers=_headers())
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"retryable {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts - 1:
                log.warning("图源请求失败 %s（已重试 %d 次）: %s", url, attempts, exc)
                return None
            await asyncio.sleep(0.6 * (attempt + 1) + 0.2)
    return None


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def is_life_safe(item: MediaItem, persona: dict) -> bool:
    """硬过滤：有人物的图不要；女性服饰不给男性；敏感词直接丢。"""
    blob = item.text_blob()
    if not blob.strip():
        return False
    tokens = set(re.findall(r"[a-z]+", blob))
    words = set(blob.split())
    if tokens & PERSON_WORDS or words & PERSON_WORDS:
        return False
    if tokens & SENSITIVE_WORDS or words & SENSITIVE_WORDS:
        return False
    gender = str(persona.get("gender") or "").lower()
    if gender == "male" and (tokens & FEMININE_WORDS or words & FEMININE_WORDS):
        return False
    return True


def relevant_to_query(item: MediaItem, query: str) -> bool:
    """标题/标签至少命中一个检索词（长度>=4），过滤元数据错标的图。"""
    blob = item.text_blob()
    tokens = [t for t in re.findall(r"[a-z]+", query.lower()) if len(t) >= 4]
    if not tokens:
        return True
    return any(token in blob for token in tokens)


def queries_for(persona: dict) -> list[str]:
    """按人物兴趣/职业生成检索词。"""
    raw = persona.get("interests") or []
    if isinstance(raw, str):
        try:
            import json  # noqa: PLC0415

            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = [raw]
    queries: list[str] = []
    for tag in raw if isinstance(raw, list) else []:
        key = str(tag).strip().lower()
        queries.extend(QUERY_BY_INTEREST.get(key, []))
    occupation = str(persona.get("occupation") or "").lower()
    for key, values in QUERY_BY_INTEREST.items():
        if key in occupation:
            queries.extend(values)
    return queries[:4] or FALLBACK_QUERIES


class OpenverseAdapter:
    name = "openverse"
    endpoint = "https://api.openverse.org/v1/images/"

    async def search(self, client: httpx.AsyncClient, query: str, limit: int = 6) -> list[MediaItem]:
        params = {
            "q": query,
            "page_size": max(1, min(limit, 20)),
            "license": "cc0,by,by-sa,pdm",
            "mature": "false",
            "extension": "jpg,png,webp",
        }
        data = await _get_json(client, self.endpoint, params)
        if not isinstance(data, dict):
            return []
        items: list[MediaItem] = []
        for row in data.get("results") or []:
            licence = str(row.get("license") or "").lower()
            if licence not in {"cc0", "pdm", "by", "by-sa"}:
                continue
            items.append(
                MediaItem(
                    external_id=f"openverse:{row.get('id')}",
                    source="Openverse",
                    title=str(row.get("title") or ""),
                    tags=[str(t.get("name") or "") for t in (row.get("tags") or []) if isinstance(t, dict)],
                    license=licence.upper(),
                    author=str(row.get("creator") or ""),
                    page_url=str(row.get("foreign_landing_url") or ""),
                    image_url=str(row.get("url") or ""),
                    thumb_url=str(row.get("thumbnail") or row.get("url") or ""),
                )
            )
        return items


class WikimediaAdapter:
    name = "wikimedia"
    endpoint = "https://commons.wikimedia.org/w/api.php"

    async def search(self, client: httpx.AsyncClient, query: str, limit: int = 6) -> list[MediaItem]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6",
            "gsrlimit": str(max(1, min(limit, 20))),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size",
            "iiurlwidth": "900",
        }
        data = await _get_json(client, self.endpoint, params)
        if not isinstance(data, dict):
            return []
        pages = ((data.get("query") or {}).get("pages") or {}).values()
        items: list[MediaItem] = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            licence = _strip_html(str((meta.get("LicenseShortName") or {}).get("value") or ""))
            licence_key = licence.lower()
            if not any(k in licence_key for k in ("cc0", "public domain", "cc by", "cc-by")):
                continue
            if "nc" in licence_key or "nd" in licence_key:
                continue
            items.append(
                MediaItem(
                    external_id=f"commons:{page.get('pageid')}",
                    source="Wikimedia Commons",
                    title=_strip_html(str(page.get("title") or "")).replace("File:", ""),
                    tags=[_strip_html(str((meta.get("ImageDescription") or {}).get("value") or ""))[:120]],
                    license=licence or "CC",
                    author=_strip_html(str((meta.get("Artist") or {}).get("value") or "")),
                    page_url=str(info.get("descriptionurl") or ""),
                    image_url=str(info.get("url") or ""),
                    thumb_url=str(info.get("thumburl") or info.get("url") or ""),
                )
            )
        return items


async def download_image(client: httpx.AsyncClient, item: MediaItem, media_dir: str) -> str | None:
    """下载并压缩成 900px webp，返回 /media/xxx.webp；失败返回 None。

    流式读取 + 单文件字节上限（``SE_MEDIA_DOWNLOAD_MAX_BYTES``，默认 12MB）：
    超大图直接放弃，避免把整张原图读进内存。
    """
    os.makedirs(media_dir, exist_ok=True)
    url = item.thumb_url or item.image_url
    if not url:
        return None
    try:
        max_bytes = int(float(os.getenv("SE_MEDIA_DOWNLOAD_MAX_BYTES", "") or 12 * 1024 * 1024))
    except (TypeError, ValueError):
        max_bytes = 12 * 1024 * 1024
    data = bytearray()
    try:
        async with client.stream("GET", url, headers=_headers(), timeout=30.0) as resp:
            resp.raise_for_status()
            if not str(resp.headers.get("content-type", "")).startswith("image/"):
                return None
            declared = int(resp.headers.get("content-length") or 0)
            if declared and declared > max_bytes:
                log.warning("图片过大（%d bytes），跳过 %s", declared, url)
                return None
            async for chunk in resp.aiter_bytes(65536):
                data.extend(chunk)
                if len(data) > max_bytes:
                    log.warning("图片下载超过上限（%d bytes），放弃 %s", max_bytes, url)
                    return None
        image = Image.open(io.BytesIO(bytes(data))).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.warning("图片下载失败 %s: %s", url, exc)
        return None
    digest = hashlib.sha1(item.external_id.encode("utf-8")).hexdigest()[:16]
    filename = f"{digest}.webp"
    path = os.path.join(media_dir, filename)
    if not os.path.exists(path):
        width, height = image.size
        if max(width, height) > 900:
            image.thumbnail((900, 900))
        image.save(path, "WEBP", quality=82, method=4)
    return f"/media/{filename}"


def credit_text(item: MediaItem) -> str:
    parts = [p for p in (item.author, item.source) if p]
    text = " · ".join(parts) if parts else item.source
    if item.license:
        text += f" · {item.license}"
    return text


__all__ = [
    "MediaItem",
    "OpenverseAdapter",
    "WikimediaAdapter",
    "credit_text",
    "download_image",
    "is_life_safe",
    "queries_for",
]
