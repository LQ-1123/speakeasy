"""对话历史接口：查询 / 清空 + 图片访问。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api", tags=["conversations"])


def _store(request: Request) -> Any:
    store = getattr(request.app.state, "conversation_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="对话存储未初始化")
    return store


@router.get("/conversations")
async def list_conversations(request: Request) -> dict[str, Any]:
    """每个角色的对话摘要（条数 / 最后一句 / 更新时间）。"""
    return {"ok": True, "items": _store(request).summary()}


@router.get("/conversations/{persona_id}")
async def get_conversation(persona_id: str, request: Request) -> dict[str, Any]:
    msgs = _store(request).load(persona_id)
    return {"ok": True, "persona_id": persona_id, "messages": msgs, "count": len(msgs)}


@router.delete("/conversations/{persona_id}")
async def clear_conversation(persona_id: str, request: Request) -> dict[str, Any]:
    removed = _store(request).clear(persona_id)
    return {"ok": True, "persona_id": persona_id, "removed": removed}


@router.get("/uploads/{name}")
async def get_upload(name: str, request: Request) -> FileResponse:
    """对话里引用的图片。名字只允许 sha1 风格的十六进制 + 扩展名。"""
    store = _store(request)
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = os.path.join(store.upload_dir, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)
