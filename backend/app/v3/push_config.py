"""JPush 凭据的运行时存储（管理台可改）。

为什么需要它
------------
JPush 的 AppKey / MasterSecret 原本只能通过环境变量或 ``.env`` 提供，
改一次要重启服务。管理台既然要「配置推送」，就必须能在线上改。

优先级：**环境变量 > 这里的运行时值 > settings（.env）**。
与环境变量优先保持一致（世界节奏参数也是这个顺序）：运维设了 env 说明
"这个我需要强制控制"，就不该被后台的值悄悄盖掉。

安全：``data/jpush_config.json`` 里是明文密钥，与 ``deepgram_config.json``
同一处理方式（本机文件、不进版本库）。接口只回**指纹**，不回明文。
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

_FILE_NAME = "jpush_config.json"


def _path() -> str:
    from app.config import get_settings  # noqa: PLC0415

    return os.path.join(str(get_settings().data_path), _FILE_NAME)


def runtime() -> dict[str, str]:
    """读运行时保存的值（没有就空串）。读失败当作没配置。"""
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001 - 文件不存在 / 损坏都算没配置
        return {"app_key": "", "master_secret": ""}
    if not isinstance(data, dict):
        return {"app_key": "", "master_secret": ""}
    return {
        "app_key": str(data.get("app_key") or "").strip(),
        "master_secret": str(data.get("master_secret") or "").strip(),
    }


def save_runtime(app_key: str, master_secret: str) -> bool:
    """写运行时凭据。两者都为空 = 清空。写失败返回 False。"""
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "app_key": str(app_key or "").strip(),
            "master_secret": str(master_secret or "").strip(),
        }
        if not payload["app_key"] and not payload["master_secret"]:
            if os.path.isfile(path):
                os.remove(path)
            return True
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        log.warning("写 jpush_config.json 失败: %s", exc)
        return False


def effective() -> dict[str, str]:
    """当前真正生效的凭据（含来源），供推送发送与状态展示共用。

    与 :func:`dispatch_push` 之前的内联逻辑等价，只是多了一层运行时值，
    并把优先级集中在这里——两处各写一遍必然会漂移。
    """
    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    stored = runtime()
    env_key = str(os.getenv("JPUSH_APP_KEY") or "").strip()
    env_secret = str(os.getenv("JPUSH_MASTER_SECRET") or "").strip()

    if env_key and env_secret:
        return {"app_key": env_key, "master_secret": env_secret, "source": "env"}
    if stored["app_key"] and stored["master_secret"]:
        return {
            "app_key": stored["app_key"],
            "master_secret": stored["master_secret"],
            "source": "admin",
        }
    settings_key = str(getattr(settings, "jpush_app_key", "") or "").strip()
    settings_secret = str(getattr(settings, "jpush_master_secret", "") or "").strip()
    if settings_key and settings_secret:
        return {
            "app_key": settings_key,
            "master_secret": settings_secret,
            "source": "settings",
        }
    # 部分配置也算"没配好"：缺一半的密钥只会在发送时莫名失败
    return {"app_key": "", "master_secret": "", "source": ""}


def status() -> dict[str, Any]:
    """给管理台的脱敏状态（**不返回明文密钥**）。"""
    values = effective()
    key = values["app_key"]
    return {
        "configured": bool(values["app_key"] and values["master_secret"]),
        "source": values["source"],
        "app_key_tail": key[-4:] if key else "",
        "has_master_secret": bool(values["master_secret"]),
    }


__all__ = ["effective", "runtime", "save_runtime", "status"]
