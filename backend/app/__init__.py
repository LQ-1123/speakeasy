"""SpeakEasy 后端。

本地语音栈（Silero-VAD + faster-whisper + CosyVoice2/F5-TTS）+ 云端 LLM 的
实时英语口语对练服务。

子包:
    app.voice    本地语音推理（VAD / STT / TTS / 参考音频）
    app.llm      外部 LLM 调度（流式客户端 / 断句 / 润色建议）
    app.pipeline 回合编排与指标
    app.routers  FastAPI 路由（WebSocket / HTTP）
    app.personas 角色模板库
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
