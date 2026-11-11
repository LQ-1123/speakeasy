"""管理 API · 词库与运行时状态（``/api/admin/lexicons/*``、``/status``、``/usage``、
``/simulate``）。

从 ``app/admin/router.py`` 原样迁移，**路径与返回结构完全不变**：
``GET /status``、``GET/PUT /lexicons/{name}``、``POST /lexicons/{name}/validate``、
``POST /lexicons/{name}/reset``、``GET /lexicons``、``GET /usage``、``GET /simulate``。

注意：本模块的 router **不带前缀**（``/status``、``/simulate`` 直接挂在 ``/api/admin`` 下），
词库相关路径各自写全 ``/lexicons...``。
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from app.admin.api import deps
from app.logging_utils import get_logger
from app.v3 import lexicon

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter()

def _summary() -> list[dict[str, Any]]:
    """各词库文件的落地状态（存在性 / 体积 / mtime / 校验问题）。

    ``lexicon.load`` 读的是磁盘缓存，``_summary`` 保持与迁移前一致（不做 fresh 读），
    以免管理台状态查询频繁触发磁盘扫描。
    """
    items: list[dict[str, Any]] = []
    for name, title in lexicon.LEXICON_FILES.items():
        path = lexicon.path_for(name)
        exists = os.path.exists(path)
        issues = lexicon.validate(name, lexicon.load(name)) if exists else []
        items.append(
            {
                "name": name,
                "title": title,
                "exists": exists,
                "path": path,
                "bytes": os.path.getsize(path) if exists else 0,
                "mtime": os.path.getmtime(path) if exists else 0.0,
                "issues": issues,
                "ok": exists and not issues,
            }
        )
    return items

@router.get("/status")
async def admin_status(request: Request) -> dict:
    return {
        "applied": lexicon.applied_status(),
        "lexicons": _summary(),
        "server_time": time.time(),
    }

@router.get("/lexicons")
async def list_lexicons(request: Request) -> dict:
    return {"items": _summary()}

@router.get("/lexicons/{name}")
async def get_lexicon(request: Request, name: str) -> dict:
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    data = lexicon.load(name, fresh=True)
    return {
        "name": name,
        "title": lexicon.LEXICON_FILES[name],
        "exists": data is not None,
        "data": data or {},
        "issues": lexicon.validate(name, data) if data is not None else ["词库文件不存在"],
        "path": lexicon.path_for(name),
    }

@router.put("/lexicons/{name}")
async def put_lexicon(
    request: Request, name: str, payload: dict = Body(...)
) -> dict:
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="body 必须是 {'data': {...}}")
    try:
        result = lexicon.save(name, data)
    except lexicon.LexiconError as exc:
        return {
            "ok": False,
            "issues": exc.issues,
            "message": str(exc),
        }
    log.info("词库已保存: %s -> %s", name, result["path"])
    return {"ok": True, **result, "applied": lexicon.applied_status()}

@router.post("/lexicons/{name}/validate")
async def validate_lexicon(
    request: Request, name: str, payload: dict = Body(...)
) -> dict:
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    data = payload.get("data")
    issues = lexicon.validate(name, data)
    return {"ok": not issues, "issues": issues}

@router.post("/lexicons/{name}/reset")
async def reset_lexicon(request: Request, name: str) -> dict:
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    result = lexicon.reset_to_default(name)
    return {"ok": True, **result, "applied": lexicon.applied_status()}

@router.get("/usage")
async def lexicon_usage(request: Request) -> dict:
    return lexicon.usage_stats(deps.db(request))

@router.get("/simulate")
async def simulate_world(request: Request) -> dict:
    """按当前词库参数估算今天会产出多少事件/动态/评论（只读，不写库、不调模型）。"""
    return lexicon.simulate_today(deps.db(request))

__all__ = ["router"]
