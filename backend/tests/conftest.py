"""pytest 公共 fixture。

所有测试都必须在**没有 torch / faster-whisper / 模型权重**的机器上通过，
因此统一使用 mock/tone/energy 后端。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 测试不调用真实 Fish TTS（避免网络/计费），走 edge/本地合成兜底
os.environ.setdefault("SE_TTS_FISH", "0")

# 测试离线：不抓开放图库（Wikimedia/Openverse 在无网环境下会一直等超时，
# 曾导致 test_v3_world 整体挂起）。纯文字生活动态仍会生成，测试语义不受影响。
# 需要真图的用例请显式 monkeypatch.setenv("SE_MEDIA_ENABLED", "1")。
os.environ.setdefault("SE_MEDIA_ENABLED", "0")

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
def _clear_settings_cache(tmp_path_factory):
    """确保 config 在环境变量设置后重新读取。"""
    from app import config

    previous_data = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(tmp_path_factory.mktemp("runtime-data"))
    config.get_settings.cache_clear()
    yield
    if previous_data is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = previous_data
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_voice_policy(monkeypatch):
    # Individual production-policy tests opt in; lifecycle startup must not leak
    # a process-wide policy into unrelated legacy mock fixtures.
    monkeypatch.setenv("SE_VOICE_STRICT", "0")


@pytest.fixture
def settings():
    from app import config

    config.get_settings.cache_clear()
    return config.get_settings()


@pytest.fixture(scope="session")
def personas():
    from app.personas import get_personas

    return get_personas()
