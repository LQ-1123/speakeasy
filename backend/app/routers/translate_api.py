"""翻译接口：把英文回复译成中文，供学习者对照。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["translate"])


def _llm(request: Request) -> Any:
    engines = getattr(request.app.state, "engines", None)
    if engines is None or getattr(engines, "llm", None) is None:
        raise HTTPException(status_code=503, detail="LLM 引擎尚未初始化")
    return engines.llm


class TranslateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    target: str = Field("zh", description="目标语言，默认简体中文")


@router.post("/translate")
async def translate_text(request: Request, payload: TranslateIn) -> dict[str, Any]:
    """把一段英文译成中文。失败不抛 5xx，返回 ok=false 让前端静默降级。"""
    from app.llm.translate import translate  # noqa: PLC0415

    result = await translate(payload.text, client=_llm(request), target=payload.target)
    return result


class TranslateBatchIn(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=20)
    target: str = "zh"


@router.post("/translate/batch")
async def translate_batch(request: Request, payload: TranslateBatchIn) -> dict[str, Any]:
    """批量翻译（一次请求译多条，比如整段对话）。"""
    import asyncio  # noqa: PLC0415

    from app.llm.translate import translate  # noqa: PLC0415

    llm = _llm(request)
    results = await asyncio.gather(
        *(translate(t, client=llm, target=payload.target) for t in payload.texts),
        return_exceptions=False,
    )
    return {"ok": True, "items": list(results)}
