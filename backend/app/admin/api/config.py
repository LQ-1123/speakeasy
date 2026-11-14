"""管理 API · 模型与语音服务配置（``/api/admin/config/*``）。

从 ``app/admin/router.py`` 原样迁移，**路径与返回结构完全不变**：
``GET /config``、``PUT /config/{target}``、``POST /config/{target}/test``。

target 取值：``llm`` / ``stt`` / ``tts`` / ``deepgram``。未知 target 一律 404，
与迁移前一致（而不是新接口约定的 422）——旧契约保持兼容。
"""

from __future__ import annotations

import asyncio

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
    pool = getattr(deps.services(request), 'provider_pool', None)
    provider = {'tts':'fish', 'stt':'siliconflow'}.get(target, target)
    secret_fields = {'api_key', 'fish_api_key', 'fish_base_url', 'base_url'}
    if pool is not None and provider in ('llm', 'fish', 'deepgram', 'siliconflow'):
        if await asyncio.to_thread(pool.store.provider_enabled, provider) and any(payload.get(key) for key in secret_fields):
            raise HTTPException(status_code=409, detail='资源池已启用，请在账号资源中修改凭据；回滚后可编辑原配置')
        model = payload.get('fish_model') if target == 'tts' else payload.get('model')
        if model and pool.applies(provider, model):
            # Default-model changes select an already verified route. Testing
            # a legacy endpoint/key here would reject a valid pool-only setup.
            payload = {**payload, 'skip_verify': True}
    return await handler(deps.services(request), payload)

@router.post("/{target}/test")
async def test_config(request: Request, target: str) -> dict:
    """用当前配置做连通性测试。"""
    handler = _TEST_HANDLERS.get(target)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"未知配置项: {target}")
    pool = getattr(deps.services(request), 'provider_pool', None)
    provider = {'tts':'fish', 'stt':'siliconflow'}.get(target, target)
    if pool is not None and provider in ('llm', 'fish', 'deepgram', 'siliconflow') and await asyncio.to_thread(pool.store.provider_enabled, provider):
        raise HTTPException(status_code=409, detail='请对资源池中的具体账号执行测试，避免测试到原有单 Key')
    try:
        return await handler(deps.services(request))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

__all__ = ["router"]
