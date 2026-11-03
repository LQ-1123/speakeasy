"""声音克隆上传 API：用户上传 30s–1min 录音 → 定制音色。

端点
----
| Method | Path | 说明 |
|---|---|---|
| GET | ``/api/voice/status`` | XTTS 是否可用 + 已上传音色列表 |
| POST | ``/api/voice/upload`` | 上传音频（multipart），规范化后登记为音色 |
| POST | ``/api/voice/upload-blob`` | 上传原始 PCM（前端直接录的，免转码） |
| DELETE | ``/api/voice/{voice_id}`` | 删除音色 |
| GET | ``/api/voice/preview/{voice_id}`` | 用该音色合成一句试听 |
| POST | ``/api/voice/assign`` | 把音色绑定到某个角色（persona） |

设计要点
--------
- **音频规范化**：上传的 mp3/m4a/wav 统一转 22.05k mono wav，去静音、峰值归一化。
- **时长约束**：3s 起（XTTS 下限），30s–1min 最佳；超过 2 分钟拒绝（可用 ``max_s`` 截断）。
- **持久化**：音色存 ``data/voices/<voice_id>.wav``，重启后自动重新载入。
- **安全**：文件名只允许 ``[A-Za-z0-9_-]``，避免路径穿越。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.logging_utils import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])

#: voice_id 允许的字符（防止路径穿越）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: 上传体积上限
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB


def _engines(request: Request) -> Any:
    engines = getattr(request.app.state, "engines", None)
    if engines is None:
        raise HTTPException(status_code=503, detail="引擎尚未初始化")
    return engines


def _xtts(engines: Any):
    """取 XTTS 引擎（惰性创建，挂在 engines 上）。"""
    from app.voice.xtts_engine import XTTSEngine  # noqa: PLC0415

    xtts = getattr(engines, "xtts", None)
    if xtts is None:
        voices_dir = os.path.join(str(engines.settings.data_path), "voices")
        xtts = XTTSEngine()
        xtts.load_voices_from_dir(voices_dir)
        engines.xtts = xtts
    return xtts


def _openvoice(engines: Any):
    """取 OpenVoice 引擎；不可用则返回 None。

    OpenVoice 是「保留口音的克隆」的关键：edge 母语声线 → 换音色。
    """
    ov = getattr(engines, "openvoice", None)
    if ov is None:
        return None
    try:
        if not type(ov).looks_available():
            return None
    except Exception:  # noqa: BLE001
        return None
    return ov


def _sync_timbres(engines: Any) -> None:
    """把 voices/ 下的录音同步给 OpenVoice 当"取音色"的参考。"""
    ov = _openvoice(engines)
    if ov is None:
        return
    try:
        ov.load_timbres_from_dir(_voices_dir(engines))
    except Exception as exc:  # noqa: BLE001
        log.debug("同步 OpenVoice 音色失败: %s", exc)


def _voices_dir(engines: Any) -> str:
    d = os.path.join(str(engines.settings.data_path), "voices")
    os.makedirs(d, exist_ok=True)
    return d


def _validate_id(voice_id: str) -> str:
    vid = (voice_id or "").strip()
    if not _ID_RE.match(vid):
        raise HTTPException(
            status_code=400,
            detail="voice_id 只能包含字母、数字、下划线、连字符（1–64 字符）",
        )
    return vid


# --------------------------------------------------------------------------- #
# GET /api/voice/status
# --------------------------------------------------------------------------- #
@router.get("/status")
async def voice_status(request: Request) -> dict[str, Any]:
    """XTTS 可用性 + 已上传音色。前端据此显示"上传录音定制音色"入口。"""
    engines = _engines(request)
    try:
        from app.voice.xtts_engine import MAX_REF_S, MIN_REF_S, XTTSEngine  # noqa: PLC0415

        available = XTTSEngine.looks_available()
        min_s, max_s = MIN_REF_S, MAX_REF_S
    except Exception:  # noqa: BLE001
        available = False
        min_s, max_s = 3.0, 120.0

    # 同步 OpenVoice 音色（让 cascade 能用上已上传的录音）
    _sync_timbres(engines)
    ov = _openvoice(engines)

    voices: list[dict[str, Any]] = []
    if available:
        try:
            xtts = _xtts(engines)
            voices = [
                {
                    "voice_id": v.voice_id,
                    "duration_s": round(v.duration_s, 2),
                    "created_at": v.created_at,
                }
                for v in xtts.voices()
            ]
        except Exception as exc:  # noqa: BLE001
            log.debug("读取音色列表失败: %s", exc)

    # 角色 → 音色绑定
    assign = getattr(engines, "voice_assignments", None) or {}

    return {
        "available": available,
        "backend": "cascade (edge + openvoice)" if ov is not None else "xtts-v2",
        # 关键能力标志：True = 克隆能保留真实口音（级联），False = 只剩音色
        "accent_preserving": ov is not None,
        "min_duration_s": min_s,
        "max_duration_s": max_s,
        "recommended": "10–30 秒（最多 1 分钟）",
        "voices": voices,
        "assignments": dict(assign),
        "personas": [
            {
                "persona_id": pid,
                "accent": _persona_accent(pid),
                "accent_source": _persona_accent_source(pid),
                "edge_voice": _persona_edge_voice(pid),
            }
            for pid in _known_persona_ids(engines)
        ],
        "note": (
            "上传 10–30 秒清晰英语录音，即可用你的音色说话。"
            "口音来自角色的母语声线，会完整保留。"
            if ov is not None
            else "上传 10–30 秒清晰英语录音，即可用你的音色说话。"
            "（OpenVoice 不可用：口音将无法保留）"
        ),
    }


def _persona_accent(persona_id: str) -> str:
    """角色的口音说明（单一来源：EdgeVoice.accent_label）。"""
    try:
        from app.voice.edge_tts_engine import resolve_voice  # noqa: PLC0415

        v = resolve_voice(persona_id)
        return v.accent_label or v.style or "English"
    except Exception:  # noqa: BLE001
        return "English"


def _persona_accent_source(persona_id: str) -> str:
    """口音来源：official = 官方地区变体；foreign_fallback = 外语声线兜底。"""
    try:
        from app.voice.edge_tts_engine import resolve_voice  # noqa: PLC0415

        return resolve_voice(persona_id).accent_source
    except Exception:  # noqa: BLE001
        return "official"


def _persona_edge_voice(persona_id: str) -> str:
    try:
        from app.voice.edge_tts_engine import resolve_voice  # noqa: PLC0415

        return resolve_voice(persona_id).voice
    except Exception:  # noqa: BLE001
        return ""


def _known_persona_ids(engines: Any) -> list[str]:
    try:
        return [p.id for p in engines.personas.all()]
    except Exception:  # noqa: BLE001
        from app.voice.edge_tts_engine import PERSONA_VOICES  # noqa: PLC0415

        return list(PERSONA_VOICES)


# --------------------------------------------------------------------------- #
# GET /api/voice/accents  —— 可用的全部口音声线
# --------------------------------------------------------------------------- #
@router.get("/accents")
async def voice_accents() -> dict[str, Any]:
    """列出所有可用口音声线。

    两类：
      official        —— 官方英语变体（真实地区口音，**首选**）
      foreign_fallback —— 外语声线读英文（口音浓，但英文单词可能读不准）
    """
    from app.voice.edge_tts_engine import (  # noqa: PLC0415
        FOREIGN_FALLBACK_VOICES,
        OFFICIAL_EN_VARIANTS,
    )

    official = [
        {"locale": loc, "voice": v, "kind": "official"}
        for loc, vs in OFFICIAL_EN_VARIANTS.items()
        for v in vs
    ]
    fallback = [
        {"locale": loc, "voice": v, "kind": "foreign_fallback"}
        for loc, vs in FOREIGN_FALLBACK_VOICES.items()
        for v in vs
    ]
    return {
        "official": official,
        "foreign_fallback": fallback,
        "policy": (
            "优先使用官方英语变体（真实地区口音，100% 自然）。"
            "只有目标地区没有官方英语变体时（如意大利、法国、德国、日本），"
            "才用该语言的母语声线读英文作兜底 —— 口音浓但个别词发音可能不准。"
        ),
        "counts": {
            "official": len(official),
            "foreign_fallback": len(fallback),
        },
    }


# --------------------------------------------------------------------------- #
# POST /api/voice/upload  （multipart）
# --------------------------------------------------------------------------- #
@router.post("/upload")
async def upload_voice(
    request: Request,
    file: UploadFile = File(..., description="音频文件（wav/mp3/m4a/flac/ogg）"),
    voice_id: str = Form(..., description="音色标识，如 my_voice 或 persona_id"),
    ref_text: str = Form("", description="可选：录音的逐字转写（提高克隆稳定性）"),
) -> dict[str, Any]:
    """上传音频 → 规范化 → 登记为可用音色。"""
    engines = _engines(request)

    try:
        from app.voice.xtts_engine import XTTSEngine, XTTSError, normalize_reference  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"XTTS 不可用: {exc}") from exc

    if not XTTSEngine.looks_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "XTTS 未安装。请运行：\n"
                '  D:\\speakeasy-venv\\Scripts\\python.exe -m pip install coqui-tts torchcodec six\n'
                "（本机已装好，如报错请检查）"
            ),
        )

    vid = _validate_id(voice_id)

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(raw)/1024/1024:.1f}MB），上限 {MAX_UPLOAD_BYTES//1024//1024}MB",
        )

    suffix = os.path.splitext(file.filename or "")[1].lower() or ".wav"
    if suffix not in (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".aac", ".opus"):
        raise HTTPException(status_code=400, detail=f"不支持的音频格式: {suffix}")

    import tempfile

    src = tempfile.mktemp(suffix=suffix)
    try:
        with open(src, "wb") as f:
            f.write(raw)

        dst = os.path.join(_voices_dir(engines), f"{vid}.wav")
        try:
            # 用 ffmpeg 统一转码（支持 mp3/m4a/webm 等）
            info = await _transcode_and_normalize(src, dst)
        except XTTSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        xtts = _xtts(engines)
        voice = xtts.register_voice(vid, dst, ref_text=ref_text)
        # 同时登记给 OpenVoice：cascade 克隆用它取音色（保留真实口音）
        _sync_timbres(engines)

        log.info(
            "音色已登记 voice_id=%s duration=%.2fs trimmed=%s",
            vid, info["duration_s"], info["trimmed"],
        )
        return {
            "ok": True,
            "voice_id": vid,
            "duration_s": voice.duration_s,
            "trimmed": info["trimmed"],
            "sample_rate": info["sample_rate"],
            "message": f"音色已就绪（{voice.duration_s:.1f}s）",
        }
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


async def _transcode_and_normalize(src: str, dst: str) -> dict[str, Any]:
    """先用 ffmpeg 转 wav（若需要），再做规范化。"""
    import asyncio
    import subprocess

    from app.voice.xtts_engine import normalize_reference  # noqa: PLC0415

    # 先探测能否被 soundfile 直接读；不能则用 ffmpeg 转
    tmp_wav = src
    need_ffmpeg = os.path.splitext(src)[1].lower() not in (".wav", ".flac", ".ogg")
    if not need_ffmpeg:
        try:
            import soundfile as sf  # noqa: PLC0415

            sf.info(src)
            need_ffmpeg = False
        except Exception:  # noqa: BLE001
            need_ffmpeg = True

    if need_ffmpeg:
        import tempfile

        tmp_wav = tempfile.mktemp(suffix=".wav")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src,
            "-ar", "22050", "-ac", "1", "-acodec", "pcm_s16le",
            tmp_wav,
        ]
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, timeout=180, check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"音频转码失败（需要 ffmpeg）: {proc.stderr[:300].decode('utf-8', 'ignore')}"
            )

    try:
        return await asyncio.to_thread(normalize_reference, tmp_wav, dst)
    finally:
        if tmp_wav != src:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# POST /api/voice/upload-blob  （前端 MediaRecorder 原始字节）
# --------------------------------------------------------------------------- #
@router.post("/upload-blob")
async def upload_voice_blob(
    request: Request,
    voice_id: str,
    format: str = "webm",
    ref_text: str = "",
) -> dict[str, Any]:
    """接收原始音频字节（Content-Type 任意），适合前端直接 POST body。"""
    engines = _engines(request)
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="请求体为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大")

    vid = _validate_id(voice_id)
    fmt = (format or "webm").lower().lstrip(".")
    if fmt not in ("wav", "mp3", "m4a", "flac", "ogg", "webm", "aac", "opus"):
        raise HTTPException(status_code=400, detail=f"不支持的格式: {fmt}")

    import tempfile

    src = tempfile.mktemp(suffix=f".{fmt}")
    try:
        with open(src, "wb") as f:
            f.write(raw)
        dst = os.path.join(_voices_dir(engines), f"{vid}.wav")
        info = await _transcode_and_normalize(src, dst)
        xtts = _xtts(engines)
        voice = xtts.register_voice(vid, dst, ref_text=ref_text)
        # 同时登记给 OpenVoice：cascade 克隆用它取音色（保留真实口音）
        _sync_timbres(engines)
        return {
            "ok": True,
            "voice_id": vid,
            "duration_s": voice.duration_s,
            "trimmed": info["trimmed"],
            "message": f"音色已就绪（{voice.duration_s:.1f}s）",
        }
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# POST /api/voice/preview/{voice_id}
# --------------------------------------------------------------------------- #
class PreviewIn(BaseModel):
    text: str = Field(
        default="Right then, that's a flat white for you. Cheers, guv.",
        description="试听文本",
    )


@router.post("/preview/{voice_id}")
async def preview_voice(
    request: Request,
    voice_id: str,
    payload: PreviewIn,
    persona_id: str = "",
) -> Response:
    """用该音色合成一句，直接返回 wav 供前端播放。

    走**级联**（edge 该角色母语声线 → OpenVoice 换音色），所以试听听到的就是
    真实效果：口音来自角色，音色来自上传的录音。
    加 ``?persona_id=raj`` 可指定用哪种口音试听。
    """
    engines = _engines(request)
    vid = _validate_id(voice_id)
    try:
        xtts = _xtts(engines)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not xtts.get_voice(vid):
        raise HTTPException(status_code=404, detail=f"音色 {vid} 未上传")

    ov = _openvoice(engines)
    text = payload.text[:300]
    try:
        if ov is not None and ov.get_timbre(vid) is not None:
            # 级联：保留口音
            from app.voice.edge_tts_engine import EdgeTTSEngine, trim_silence  # noqa: PLC0415

            pid = persona_id or "oliver"
            edge = EdgeTTSEngine(sample_rate=24000)
            await edge.ensure_ready()
            accented = await edge.synthesize(text, pid)
            arr = await ov.convert(accented, vid)
            arr = trim_silence(arr, 24000, head_s=0.02, tail_s=0.05)
            sr = 24000
        else:
            arr = await xtts.synthesize(text, vid)
            sr = xtts.sample_rate
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"合成失败: {exc}") from exc

    import io

    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, arr, sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


# --------------------------------------------------------------------------- #
# DELETE /api/voice/{voice_id}
# --------------------------------------------------------------------------- #
@router.delete("/{voice_id}")
async def delete_voice(request: Request, voice_id: str) -> dict[str, Any]:
    engines = _engines(request)
    vid = _validate_id(voice_id)
    try:
        xtts = _xtts(engines)
        ok = xtts.drop_voice(vid)
        ov = _openvoice(engines)
        if ov is not None:
            ov.drop_timbre(vid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"音色 {vid} 不存在")
    return {"ok": True, "message": f"音色 {vid} 已删除"}


# --------------------------------------------------------------------------- #
# POST /api/voice/assign
# --------------------------------------------------------------------------- #
class AssignIn(BaseModel):
    persona_id: str = Field(..., description="角色 id，如 oliver")
    voice_id: str = Field(..., description="已上传的音色 id；空串=取消绑定")


@router.post("/assign")
async def assign_voice(request: Request, payload: AssignIn) -> dict[str, Any]:
    """把上传的音色绑定到某个角色，之后该角色用你的音色说话。"""
    engines = _engines(request)

    if not hasattr(engines, "voice_assignments") or engines.voice_assignments is None:
        engines.voice_assignments = {}
    assignments: dict[str, str] = engines.voice_assignments

    if payload.voice_id:
        try:
            xtts = _xtts(engines)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not xtts.get_voice(payload.voice_id):
            raise HTTPException(status_code=404, detail=f"音色 {payload.voice_id} 不存在")
        assignments[payload.persona_id] = payload.voice_id
        msg = f"角色 {payload.persona_id} 将使用你的音色 {payload.voice_id}"
    else:
        assignments.pop(payload.persona_id, None)
        msg = f"角色 {payload.persona_id} 已恢复默认音色"

    log.info("音色绑定: %s", assignments)

    # 同步给 TTS 引擎（这样下一句立刻用克隆音色）
    try:
        engines.xtts = _xtts(engines)
        # 关键：把 OpenVoice 也注进去，TTS 才会走「保留口音」的级联路径，
        # 否则会退化成 XTTS 直克隆（口音被洗掉）。
        ov = _openvoice(engines)
        _sync_timbres(engines)
        tts = getattr(engines, "tts", None)
        if tts is not None:
            tts.bind_clone_engine(engines.xtts)
            tts.bind_openvoice(ov)
            tts.set_clone_assignments(assignments)
    except Exception as exc:  # noqa: BLE001
        log.warning("同步克隆绑定到 TTS 失败: %s", exc)

    return {"ok": True, "assignments": dict(assignments), "message": msg}
