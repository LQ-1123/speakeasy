"""pytest 公共 fixture。

所有测试都必须在**没有 torch / faster-whisper / 模型权重**的机器上通过，
因此统一使用 mock/tone/energy 后端。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import os

# 测试不调用真实 Fish TTS（避免网络/计费），走 edge/本地合成兜底
os.environ.setdefault("SE_TTS_FISH", "0")

# 历史测试会直接调用客户端 /api/v2/settings/*（产品上已锁死，只允许管理端改）。
# 测试环境放开编辑权限；管理端的锁定行为由 test_admin_config.py 显式验证。
os.environ.setdefault("ALLOW_KEY_EDIT", "1")


ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 测试期间强制降级后端，避免意外加载重模型
os.environ.setdefault("STT_BACKEND", "mock")
os.environ.setdefault("TTS_BACKEND", "tone")
os.environ.setdefault("VAD_BACKEND", "energy")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("TTS_PREWARM", "false")


@pytest.fixture(scope="session", autouse=True)
def _clear_settings_cache():
    """确保 config 在环境变量设置后重新读取。"""
    from app import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def settings():
    from app import config

    config.get_settings.cache_clear()
    return config.get_settings()


@pytest.fixture(scope="session")
def personas():
    from app.personas import get_personas

    return get_personas()