"""管理 API · 模型与语音服务配置（``/api/admin/config/*``）。

从 ``app/admin/router.py`` 原样迁移，**路径与返回结构完全不变**：
``GET /config``、``PUT /config/{target}``、``POST /config/{target}/test``。

target 取值：``llm`` / ``stt`` / ``tts`` / ``deepgram``。未知 target 一律 404，
与迁移前一致（而不是新接口约定的 422）——旧契约保持兼容。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from app.admin import config as admin_config
from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter(prefix="/config")

#: 保存处理器：验证通过才落盘（force=true 可强存），由 admin_config 内部保证。
_SAVE_HANDLERS = {
    "llm": admin_config.save_llm,
    "stt": admin_config.save_stt,
    "tts": admin_config.save_tts,
    "deepgram": admin_config.save_deepgram,
    "jpush": admin_config.save_jpush,
}

#: 连通性测试处理器。
_TEST_HANDLERS = {
    "llm": admin_config.test_llm,
    "stt": admin_config.test_stt,
    "tts": admin_config.test_tts,
    "deepgram": admin_config.test_deepgram,
    "jpush": admin_config.test_jpush,
}

@router.get("")
async def get_config(request: Request) -> dict:
    """模型 / STT / TTS 配置快照（密钥只返回指纹）。"""
    return admin_config.snapshot(deps.services(request))

@router.put("/{target}")
async def put_config(request: Request, target: str, payload: dict = Body(...)) -> dict:
    """保存某类配置；默认**先验证再落盘**，验证失败不保存（force=true 可强存）。"""
    handler = _SAVE_HANDLERS.get(target)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"未知配置项: {target}")
    return await handler(deps.services(request), payload)

@router.post("/{target}/test")
async def test_config(request: Request, target: str) -> dict:
    """用当前配置做连通性测试。"""
    handler = _TEST_HANDLERS.get(target)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"未知配置项: {target}")
    try:
        return await handler(deps.services(request))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

__all__ = ["router"]
