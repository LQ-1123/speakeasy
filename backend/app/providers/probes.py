"""Small explicit business probes. No saved credential is tested at startup."""
from __future__ import annotations

import asyncio
import io
import json
import time
import wave
from urllib.parse import urlencode

import httpx
import websockets


def sample_wav() -> bytes:
    target = io.BytesIO()
    with wave.open(target, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(bytes(16000 * 2))
    return target.getvalue()


def valid_audio(data: bytes) -> bool:
    if len(data) < 512:
        return False
    if data.startswith(b'RIFF'):
        try:
            with wave.open(io.BytesIO(data), 'rb') as audio:
                frames = audio.getnframes()
                return frames > 0 and len(audio.readframes(frames)) == frames * audio.getnchannels() * audio.getsampwidth()
        except (wave.Error, EOFError):
            return False
    offset = 0
    if data.startswith(b'ID3') and len(data) >= 10:
        size = data[6:10]
        if any(byte & 0x80 for byte in size):
            return False
        offset = 10 + (size[0] << 21 | size[1] << 14 | size[2] << 7 | size[3])
    from app.voice.fish_tts import _mp3_frame_kbps
    return bool(_mp3_frame_kbps(data[offset:]))


def public_failure(status: int = 0) -> str:
    if status in (401, 403):
        return '凭据或所选模型权限不可用，请检查配置'
    if status == 402:
        return '账户额度不足'
    if status == 429:
        return '上游暂时限流，请稍后测试'
    return '上游连接或业务响应未通过验证，请检查端点与模型'


async def probe_resource(resource: dict, *, transport=None, client=None, ws_connect=None) -> dict:
    started = time.monotonic()
    provider = resource['provider']
    key = resource.get('api_key') or ''
    base = str(resource.get('base_url') or '').rstrip('/')
    model = str(resource.get('model') or '')
    owned = client is None and provider != 'deepgram'
    if owned:
        client = httpx.AsyncClient(transport=transport, timeout=15,
                                  limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
    try:
        if not key:
            return {'ok': False, 'message': '请填写完整凭据', 'status_code': 422}
        async with asyncio.timeout(15):
            if provider == 'deepgram':
                url = base.replace('https://', 'wss://').replace('http://', 'ws://')
                if not url.endswith('/listen'):
                    url += '/listen' if url.endswith('/v1') else '/v1/listen'
                params = urlencode({'model': model or 'nova-3', 'encoding': 'linear16',
                                    'sample_rate': 16000, 'channels': 1})
                connect = ws_connect or websockets.connect
                socket = await connect(url + '?' + params, additional_headers={'Authorization': f'Token {key}'})
                try:
                    await socket.send(bytes(16000 * 2))
                    await socket.send(json.dumps({'type': 'Finalize'}))
                    await socket.send(json.dumps({'type': 'CloseStream'}))
                    verified = False
                    async for raw in socket:
                        item = json.loads(raw)
                        if item.get('type') == 'Error':
                            return {'ok': False, 'message': public_failure(), 'status_code': 422}
                        if item.get('type') in ('Metadata', 'Results'):
                            verified = True
                            break
                    if not verified:
                        return {'ok': False, 'message': public_failure(), 'status_code': 422}
                finally:
                    await socket.close()
            else:
                headers = {'Authorization': f'Bearer {key}'}
                if provider == 'llm':
                    response = await client.post(base + '/chat/completions', headers=headers,
                        json={'model': model, 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                              'max_tokens': 8, 'stream': False})
                elif provider == 'fish':
                    headers['model'] = model
                    capabilities = resource.get('capabilities') or {}
                    payload = {'text': 'Hello.', 'format': 'mp3'}
                    voices = capabilities.get('voices') or []
                    if voices:
                        payload['reference_id'] = voices[0]
                    response = await client.post(base + '/v1/tts', headers=headers, json=payload)
                elif provider == 'siliconflow':
                    response = await client.post(base + '/audio/transcriptions', headers=headers,
                        data={'model': model}, files={'file': ('audio.wav', sample_wav(), 'audio/wav')})
                else:
                    return {'ok': False, 'message': '未知上游类型', 'status_code': 422}
                if response.status_code >= 400:
                    return {'ok': False, 'message': public_failure(response.status_code), 'status_code': response.status_code}
                if provider == 'fish':
                    if not valid_audio(response.content):
                        raise ValueError('invalid audio')
                else:
                    body = response.json()
                    if provider == 'llm':
                        content = body.get('choices', [{}])[0].get('message', {}).get('content')
                        if not isinstance(content, str) or not content.strip():
                            raise ValueError('empty model output')
                    elif not isinstance(body.get('text'), str):
                        raise ValueError('invalid transcription')
        return {'ok': True, 'message': '业务连接验证通过', 'status_code': 200,
                'latency_ms': round((time.monotonic() - started) * 1000, 1)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status = getattr(exc, 'status_code', 0)
        if not status:
            status = getattr(getattr(exc, 'response', None), 'status_code', 0)
        return {'ok': False, 'message': public_failure(status), 'status_code': status or 502}
    finally:
        if owned:
            await client.aclose()
