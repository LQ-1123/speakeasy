"""管理 API · Prompt 调试（``/api/admin/prompt/*``）。

从 ``app/admin/router.py`` 原样迁移，**路径与返回结构完全不变**：
``GET /prompt/versions``、``GET /prompt/preview``、``POST /prompt/preview``。

只读特性：不写库、不调 LLM。``POST`` 版本支持 ``module_overrides`` 改某个模块后重新
预览，覆盖只作用于本次响应，不落库。

注意：``prompt_composer`` / ``persona_context`` 保持**函数内延迟导入**，与迁移前一致。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter(prefix="/prompt")

#: 预览支持的 mode；与 compose_prompt 的三条链路一一对应。
_PROMPT_MODES = {"text", "image", "call"}

def _prompt_db(request: Request) -> Any:
    db = deps.db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    return db

def _prompt_preview_payload(
    request: Request,
    *,
    persona_id: str,
    user_id: str = "",
    text: str = "",
    mode: str = "text",
    module_overrides: dict[str, str] | None = None,
) -> dict:
    """组装一次调试预览：返回 modules + full_text + 本轮 plan 摘要。"""
    from app.v3 import prompt_composer  # noqa: PLC0415 - 只在管理端预览时导入

    db = _prompt_db(request)
    mode_value = str(mode or "text").strip().lower()
    if mode_value not in _PROMPT_MODES:
        raise HTTPException(status_code=400, detail=f"未知 mode: {mode}（可选 text/image/call）")
    pid = str(persona_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="persona_id 不能为空")
    row = prompt_composer.get_persona_row(db, pid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"persona 不存在: {pid}")

    friend = prompt_composer.friend_for_persona(row, pid, user_id=user_id or None)
    plan = None
    try:
        from app.v3 import persona_context  # noqa: PLC0415

        if mode_value == "call":
            plan = persona_context.build_call_plan(db, friend, str(text or ""))
        else:
            plan = persona_context.build_turn_plan(db, friend, str(text or ""))
    except Exception as exc:  # noqa: BLE001 - plan 失败仍返回降级 prompt 供排查
        log.debug("prompt preview: build plan 失败（忽略）: %s", exc)
        plan = None

    composed = prompt_composer.compose_prompt(
        db,
        friend,
        user_text=str(text or ""),
        plan=plan,
        mode=mode_value,
        memory_context="",
        module_overrides=module_overrides,
    )
    payload = composed.as_dict(include_text=True)
    # 预算裁剪记录：运营需要知道"我写的这段是不是被长度预算挤掉了"，
    # 否则会误以为改写没生效。
    payload["trimmed"] = list(getattr(composed, "trimmed", []) or [])
    payload["max_chars"] = (
        prompt_composer.MAX_CALL_CHARS
        if mode_value == "call"
        else prompt_composer.MAX_TEXT_CHARS
    )
    payload["plan"] = {
        "behavior": str(getattr(plan, "behavior", "") or ""),
        "reason": str(getattr(plan, "behavior_reason", "") or ""),
        "scene": str(getattr(plan, "scene", "") or ""),
        "activity": str((getattr(plan, "activity", None) or {}).get("label") or ""),
        "examples": len(getattr(plan, "examples", None) or []),
        "has_life_facts": bool(str(getattr(plan, "life_block", "") or "").strip()),
        "language": str(getattr(plan, "language", "") or ""),
        "degraded": bool(getattr(plan, "degraded", False)),
    }
    return payload

@router.get("/versions")
async def admin_prompt_versions(request: Request) -> dict:
    """当前 Prompt 版本 + 历史登记（团队比较用）。"""
    from app.v3 import prompt_composer  # noqa: PLC0415

    return {
        "current": prompt_composer.PROMPT_VERSION,
        "versions": list(prompt_composer.PROMPT_VERSIONS),
    }

@router.get("/preview")
async def admin_prompt_preview(
    request: Request,
    persona_id: str = "",
    user_id: str = "",
    text: str = "",
    mode: str = "text",
) -> dict:
    """只读预览某个人物在某个 mode 下的完整 system prompt。"""
    return _prompt_preview_payload(
        request, persona_id=persona_id, user_id=user_id, text=text, mode=mode
    )

@router.post("/preview")
async def admin_prompt_preview_post(request: Request, body: dict = Body(...)) -> dict:
    """调试预览（POST）：支持 module_overrides 改某个模块后重新预览，不落库。"""
    payload = body if isinstance(body, dict) else {}
    overrides = payload.get("module_overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise HTTPException(status_code=400, detail="module_overrides 必须是对象")
        for key, value in overrides.items():
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail="module_overrides 的值必须是字符串")
    return _prompt_preview_payload(
        request,
        persona_id=str(payload.get("persona_id") or ""),
        user_id=str(payload.get("user_id") or ""),
        text=str(payload.get("text") or ""),
        mode=str(payload.get("mode") or "text"),
        module_overrides=overrides,
    )

# --------------------------------------------------------------------------- #
# 持久化改写（P3）
# --------------------------------------------------------------------------- #
@router.get("/modules")
async def admin_prompt_modules(
    request: Request, persona_id: str = "", mode: str = "text"
) -> dict:
    """某人某模式的模块清单 + **当前原文** + 已保存的改写。

    ``editable`` 由后端给（来源同一份 spec），前端不要自己判断——
    否则会出现"界面允许改、实际被 compose_prompt 忽略"。

    ``modules[].text`` 是**当前真正生效的文本**（已合并已保存的改写）。
    只读模块也要给原文：运营要能看见"这段到底是什么"才知道为什么不能改，
    只给一个 key + 徽章等于什么都不说。
    """
    from app.v3 import prompt_composer, prompt_overrides  # noqa: PLC0415

    mode_value = str(mode or "text").strip().lower()
    if mode_value not in _PROMPT_MODES:
        raise HTTPException(status_code=400, detail=f"未知 mode: {mode}（可选 text/image/call）")
    pid = str(persona_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="persona_id 不能为空")
    db = _prompt_db(request)
    if prompt_composer.get_persona_row(db, pid) is None:
        raise HTTPException(status_code=404, detail=f"persona 不存在: {pid}")

    saved = prompt_overrides.load(db, pid, mode_value)
    # 复用预览链路拿到每个模块的**实际文本**（含已保存改写的合并），
    # 避免这里再写一套"如何取模块内容"的逻辑——那必然与真实组装漂移。
    preview = _prompt_preview_payload(request, persona_id=pid, mode=mode_value)
    texts = {
        str(item.get("key") or ""): str(item.get("text") or "")
        for item in (preview.get("modules") or [])
        if isinstance(item, dict)
    }
    chars = {
        str(item.get("key") or ""): int(item.get("chars") or 0)
        for item in (preview.get("modules") or [])
        if isinstance(item, dict)
    }

    modules = prompt_composer.module_meta(mode_value)
    for item in modules:
        key = item["key"]
        item["text"] = texts.get(key, "")
        item["chars"] = chars.get(key, len(item["text"]))
        item["overridden"] = key in saved

    return {
        "ok": True,
        "persona_id": pid,
        "mode": mode_value,
        "modules": modules,
        "saved": saved,
        "has_overrides": bool(saved),
        "max_chars": prompt_overrides.MAX_OVERRIDE_CHARS,
        "prompt_version": preview.get("version", ""),
        "trimmed": preview.get("trimmed", []),
    }

@router.put("/overrides")
async def admin_prompt_save_overrides(request: Request, body: dict = Body(...)) -> dict:
    """整体替换某人某模式的改写集合（传进来的就是最终状态）。

    保存后**立即生效**：``compose_prompt`` 内部会读这张表，
    所以真实聊天与通话都会用新文本，不需要重启。
    """
    from app.v3 import prompt_overrides  # noqa: PLC0415

    payload = body if isinstance(body, dict) else {}
    result = prompt_overrides.replace(
        _prompt_db(request),
        str(payload.get("persona_id") or ""),
        str(payload.get("mode") or "text"),
        payload.get("modules") if isinstance(payload.get("modules"), dict) else {},
    )
    if not result.get("ok"):
        return result
    request.state.audit_action = "prompt.overrides.save"
    request.state.audit_target = (
        f"{payload.get('persona_id') or ''}/{payload.get('mode') or 'text'}"
    )
    request.state.audit_summary = f"保存 {result['saved']} 个模块、移除 {result['removed']} 个"
    return result

@router.delete("/overrides")
async def admin_prompt_clear_overrides(
    request: Request, persona_id: str = "", mode: str = "text"
) -> dict:
    """清空某人某模式的全部改写（回到原始 prompt）。"""
    from app.v3 import prompt_overrides  # noqa: PLC0415

    result = prompt_overrides.clear(_prompt_db(request), persona_id, mode)
    if result.get("ok"):
        request.state.audit_action = "prompt.overrides.clear"
        request.state.audit_target = f"{persona_id}/{mode}"
        request.state.audit_summary = f"移除 {result.get('removed', 0)} 个模块改写"
    return result

@router.get("/templates")
async def admin_prompt_templates(request: Request) -> dict:
    """可复用模板列表。"""
    from app.v3 import prompt_overrides  # noqa: PLC0415

    return {"ok": True, "templates": prompt_overrides.list_templates(_prompt_db(request))}

@router.post("/templates")
async def admin_prompt_save_template(request: Request, body: dict = Body(...)) -> dict:
    """把一组模块改写存成模板，便于套到别的角色上。"""
    from app.v3 import prompt_overrides  # noqa: PLC0415

    payload = body if isinstance(body, dict) else {}
    result = prompt_overrides.save_template(
        _prompt_db(request),
        str(payload.get("name") or ""),
        payload.get("modules") if isinstance(payload.get("modules"), dict) else {},
        description=str(payload.get("description") or ""),
    )
    if result.get("ok"):
        request.state.audit_action = "prompt.templates.create"
        request.state.audit_target = str(result.get("id") or "")
        request.state.audit_summary = f"模板 {payload.get('name') or ''}"
    return result

@router.delete("/templates/{template_id}")
async def admin_prompt_delete_template(request: Request, template_id: str) -> dict:
    """删除模板（不影响已经套用过的角色——改写是复制过去的）。"""
    from app.v3 import prompt_overrides  # noqa: PLC0415

    result = prompt_overrides.delete_template(_prompt_db(request), template_id)
    if result.get("ok"):
        request.state.audit_action = "prompt.templates.delete"
        request.state.audit_target = template_id
        request.state.audit_summary = "删除模板"
    return result

__all__ = ["router"]
