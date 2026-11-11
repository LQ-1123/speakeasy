"""Prompt 模块改写的持久化（``prompt_overrides`` / ``prompt_templates``）。

为什么需要这一层
----------------
``prompt_composer.compose_prompt`` 早就支持 ``module_overrides`` 参数，但那是
**调试用的一次性覆盖**——传完就没了。运营想「让这个角色的开场白更冷淡一点」
并希望它对之后所有真实对话生效，就需要落库。

设计上的两个关键决定
--------------------
1. **只在 ``compose_prompt`` 内部读取已保存的改写**，不让每个调用方各传一遍。
   ``compose_prompt`` 的调用点不止一处（真实聊天、通话、管理端预览），
   靠调用方记得传，迟早漏一个——漏掉的那个就是"改了不生效"的 bug。
   在函数内部统一加载，想漏都漏不了。

2. **只有「固定模块」可改写**。模块文本的来源分两类：
   - 固定（``global`` / ``persona`` / ``first_message``）：内容不随这一轮对话变化，
     改它是安全的，改完行为可预期；
   - 动态（``plan`` / ``profile`` / ``memory``）：内容由运行时数据算出，
     改写它不只是覆盖一段文字，而是让"角色此刻在做什么"和"关系记忆"失真。
   动态模块一律**只读**，管理端会明确标注原因。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 允许改写的模块来源。见模块 docstring 第 2 点。
EDITABLE_SOURCES: frozenset[str] = frozenset({"global", "persona", "first_message"})

#: 单个模块改写文本上限。够写一段风格说明，又不至于把 prompt 撑爆。
MAX_OVERRIDE_CHARS = 4000


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def is_editable_source(source: str) -> bool:
    """该来源的模块是否允许运营改写。"""
    return str(source or "").strip().lower() in EDITABLE_SOURCES


def _lock(db: Any) -> Any:
    """复用库上的可重入锁；没有就退化成一个空上下文。"""
    return getattr(db, "_lock", None) or _NullLock()


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> bool:
        return False


def load(db: Any, persona_id: str, mode: str) -> dict[str, str]:
    """读某人某模式下已保存的模块改写（``{module_key: text}``）。

    任何异常都返回空 dict：读不到改写最多是"用回原始 prompt"，
    绝不能因为它让整轮对话失败。
    """
    pid = str(persona_id or "").strip()
    mode_value = str(mode or "").strip().lower()
    if db is None or not pid or not mode_value:
        return {}
    try:
        with _lock(db):
            rows = db.conn.execute(
                "SELECT module_key, text FROM prompt_overrides"
                " WHERE persona_id=? AND mode=?",
                (pid, mode_value),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        log.debug("读 prompt_overrides 失败（忽略）: %s", exc)
        return {}
    out: dict[str, str] = {}
    for row in rows:
        key = str(row["module_key"] or "").strip()
        text = str(row["text"] or "")
        if key and text.strip():
            out[key] = text
    return out


def replace(db: Any, persona_id: str, mode: str, modules: dict[str, str]) -> dict[str, Any]:
    """**整体替换**某人某模式的改写集合，返回 ``{ok, saved, removed, issues}``。

    为什么是整体替换而不是逐条 upsert：管理端一次改多个模块，逐条写会让
    「清空某个模块」变不明确（空串是删除还是写成空？）。整体替换语义清楚：
    传进来的就是最终状态，没出现的视为取消改写。
    """
    pid = str(persona_id or "").strip()
    mode_value = str(mode or "").strip().lower()
    issues: list[str] = []
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"], "saved": 0, "removed": 0}
    if not pid:
        return {"ok": False, "issues": ["persona_id 不能为空"], "saved": 0, "removed": 0}
    if not mode_value:
        return {"ok": False, "issues": ["mode 不能为空"], "saved": 0, "removed": 0}
    if not isinstance(modules, dict):
        return {"ok": False, "issues": ["modules 必须是对象"], "saved": 0, "removed": 0}

    cleaned: dict[str, str] = {}
    for raw_key, raw_text in modules.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        if not isinstance(raw_text, str):
            issues.append(f"{key}: 内容必须是字符串")
            continue
        text = raw_text.strip()
        if len(text) > MAX_OVERRIDE_CHARS:
            issues.append(f"{key}: 超过 {MAX_OVERRIDE_CHARS} 字（当前 {len(text)}）")
            continue
        if text:  # 空串 = 取消这一项的改写
            cleaned[key] = text
    if issues:
        return {"ok": False, "issues": issues, "saved": 0, "removed": 0}

    saved = 0
    removed = 0
    stamp = _now()
    try:
        with _lock(db), db.conn:  # noqa: SLF001 - 事务边界
            existing = {
                str(row["module_key"])
                for row in db.conn.execute(
                    "SELECT module_key FROM prompt_overrides WHERE persona_id=? AND mode=?",
                    (pid, mode_value),
                ).fetchall()
            }
            drop = existing - set(cleaned)
            if drop:
                db.conn.executemany(
                    "DELETE FROM prompt_overrides WHERE persona_id=? AND mode=? AND module_key=?",
                    [(pid, mode_value, key) for key in drop],
                )
                removed = len(drop)
            for key, text in cleaned.items():
                db.conn.execute(
                    "INSERT INTO prompt_overrides (persona_id, mode, module_key, text, updated_at)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(persona_id, mode, module_key)"
                    " DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                    (pid, mode_value, key, text, stamp),
                )
                saved += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("写 prompt_overrides 失败: %s", exc)
        return {"ok": False, "issues": [f"写入失败：{exc}"], "saved": 0, "removed": 0}
    return {"ok": True, "issues": [], "saved": saved, "removed": removed}


def clear(db: Any, persona_id: str, mode: str) -> dict[str, Any]:
    """清空某人某模式的全部改写。"""
    return replace(db, persona_id, mode, {})


def count_by_persona(db: Any) -> dict[str, int]:
    """每个角色改写了几个模块（列表页标"已改写"用）。"""
    if db is None:
        return {}
    try:
        with _lock(db):
            rows = db.conn.execute(
                "SELECT persona_id, COUNT(*) AS n FROM prompt_overrides GROUP BY persona_id"
            ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {str(r["persona_id"]): int(r["n"]) for r in rows}


# --------------------------------------------------------------------------- #
# 模板
# --------------------------------------------------------------------------- #
def list_templates(db: Any) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        with _lock(db):
            rows = db.conn.execute(
                "SELECT id, name, description, modules, created_at"
                " FROM prompt_templates ORDER BY created_at DESC"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("读 prompt_templates 失败（忽略）: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            modules = json.loads(row["modules"] or "{}")
        except (TypeError, ValueError):
            modules = {}
        if not isinstance(modules, dict):
            modules = {}
        out.append(
            {
                "id": str(row["id"]),
                "name": str(row["name"] or ""),
                "description": str(row["description"] or ""),
                "modules": modules,
                "created_at": str(row["created_at"] or ""),
            }
        )
    return out


def save_template(
    db: Any, name: str, modules: dict[str, str], *, description: str = ""
) -> dict[str, Any]:
    """新建一个模板（同名不覆盖——模板被改坏会影响所有用过它的角色）。"""
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    clean_name = str(name or "").strip()
    if not clean_name:
        return {"ok": False, "issues": ["模板名不能为空"]}
    if not isinstance(modules, dict) or not modules:
        return {"ok": False, "issues": ["模板至少要包含一个模块"]}
    bad = [k for k, v in modules.items() if not isinstance(v, str)]
    if bad:
        return {"ok": False, "issues": [f"这些模块的内容不是字符串：{bad}"]}
    tid = f"tpl_{uuid.uuid4().hex[:12]}"
    try:
        with _lock(db), db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO prompt_templates (id, name, description, modules, created_at)"
                " VALUES (?,?,?,?,?)",
                (tid, clean_name, str(description or ""), json.dumps(modules, ensure_ascii=False), _now()),
            )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"写入失败：{exc}"]}
    return {"ok": True, "issues": [], "id": tid}


def delete_template(db: Any, template_id: str) -> dict[str, Any]:
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    tid = str(template_id or "").strip()
    if not tid:
        return {"ok": False, "issues": ["id 不能为空"]}
    try:
        with _lock(db), db.conn:  # noqa: SLF001
            cur = db.conn.execute("DELETE FROM prompt_templates WHERE id=?", (tid,))
            deleted = int(getattr(cur, "rowcount", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"删除失败：{exc}"]}
    if not deleted:
        return {"ok": False, "issues": ["模板不存在"]}
    return {"ok": True, "issues": [], "deleted": deleted}


__all__ = [
    "EDITABLE_SOURCES",
    "MAX_OVERRIDE_CHARS",
    "clear",
    "count_by_persona",
    "delete_template",
    "is_editable_source",
    "list_templates",
    "load",
    "replace",
    "save_template",
]
