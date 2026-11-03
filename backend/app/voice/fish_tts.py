"""Fish Audio TTS provider。

接口形态（v1）::

    POST {base}/v1/tts
    Authorization: Bearer <key>
    model: s2.1-pro          # 也接受放在 body 里
    {"text": "...", "reference_id": "...", "format": "wav"}

支持行内情绪标记：[excited] [laughing] [whispering] [break] —— 直接写进 text 即可。
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class FishTTSError(RuntimeError):
    """Fish 调用失败。detail 里带原始响应，便于定位字段问题。"""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


async def synthesize(
    text: str,
    *,
    api_key: str,
    base_url: str = "https://api.fish.audio",
    model: str = "s2.1-pro",
    reference_id: str = "",
    audio_format: str = "wav",
    timeout: float = 120.0,
) -> bytes:
    """合成并返回音频字节。失败抛 FishTTSError（带原始 body）。"""
    key = (api_key or "").strip()
    if not key:
        raise FishTTSError("未配置 Fish API Key")

    url = (base_url or "https://api.fish.audio").rstrip("/") + "/v1/tts"
    payload: dict[str, object] = {
        "text": text,
        "format": audio_format,
        "normalize": True,
        "latency": "normal",
    }
    if reference_id.strip():
        payload["reference_id"] = reference_id.strip()

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "audio/*",
    }
    if model.strip():
        headers["model"] = model.strip()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        raise FishTTSError(f"连接 Fish 失败: {exc}") from exc

    if resp.status_code >= 400:
        body = resp.text[:600]
        raise FishTTSError(
            f"Fish 返回 {resp.status_code}", status=resp.status_code, body=body
        )
    data = resp.content
    if len(data) < 512:
        raise FishTTSError("Fish 返回的音频数据异常（过短）", body=data[:200].decode("utf-8", "ignore"))
    return data


#: 进程内缓存（音色列表不常变，10 分钟够）
_MODELS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_MODELS_TTL = 600.0


async def pick_voice_for(
    *,
    language: str = "en",
    gender: str = "",
    pages: int = 3,
    api_key: str = "",
    base_url: str = "https://api.fish.audio",
) -> str:
    """捏人时随机挑一个 Fish 音色 id（按语言 + 性别过滤）。

    过滤不到就放宽性别，再放宽语言，最后返回空串 = 用账号默认音色，
    绝不因为拿不到音色让捏人失败。
    """
    import random as _random  # noqa: PLC0415

    pool: list[dict] = []
    try:
        for page in range(1, max(1, pages) + 1):
            rows = await list_models(
                api_key=api_key, base_url=base_url, page_number=page, page_size=100
            )
            pool.extend(rows)
            if len(rows) < 100:
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("获取 Fish 音色失败（用默认音色）: %s", exc)
        return ""
    if not pool:
        return ""

    lang = (language or "").strip().lower()
    g = (gender or "").strip().lower()

    def ok(v: dict, need_lang: bool, need_gender: bool) -> bool:
        langs = [str(x).lower() for x in (v.get("languages") or [])]
        tags = [str(x).lower() for x in (v.get("tags") or [])]
        if need_lang and lang and lang not in langs:
            return False
        if need_gender and g and g not in tags:
            return False
        return True

    for need_lang, need_gender in ((True, True), (True, False), (False, False)):
        cands = [v["id"] for v in pool if ok(v, need_lang, need_gender) and v.get("id")]
        if cands:
            return _random.choice(cands)
    return ""


async def list_models(
    *,
    api_key: str,
    base_url: str = "https://api.fish.audio",
    page_number: int = 1,
    page_size: int = 100,
    use_cache: bool = True,
) -> list[dict]:
    """列出平台音色（Fish Official + 自建）。总计 1000+，按页取。

    返回字段：id / title / description / languages / tags / cover
    """
    key = (api_key or "").strip()
    if not key:
        raise FishTTSError("未配置 Fish API Key")
    import time as _time  # noqa: PLC0415

    ck = f"{base_url}|{page_number}|{page_size}|{key[-6:]}"
    if use_cache:
        hit = _MODELS_CACHE.get(ck)
        if hit and (_time.time() - hit[0]) < _MODELS_TTL:
            return hit[1]

    url = (base_url or "https://api.fish.audio").rstrip("/") + "/model"
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url, headers=headers, params={"page_size": page_size, "page_number": page_number}
        )
    if resp.status_code >= 400:
        raise FishTTSError(
            f"Fish 返回 {resp.status_code}", status=resp.status_code, body=resp.text[:400]
        )
    payload = resp.json() or {}
    out = []
    for r in payload.get("items") or []:
        out.append(
            {
                "id": str(r.get("_id") or r.get("id") or ""),
                "title": str(r.get("title") or r.get("name") or ""),
                "description": str(r.get("description") or "")[:140],
                "languages": r.get("languages") or [],
                "tags": (r.get("tags") or [])[:6],
                "cover": str(r.get("cover_image") or ""),
                "total": int(payload.get("total") or 0),
            }
        )
    if use_cache:
        _MODELS_CACHE[ck] = (_time.time(), out)
    return out
