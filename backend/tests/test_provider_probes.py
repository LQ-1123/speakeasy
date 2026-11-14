from __future__ import annotations

import json
import httpx
import pytest


@pytest.mark.asyncio
async def test_llm_probe_requires_real_model_output_and_never_exposes_secret():
    from app.providers.probes import probe_resource
    requests = []
    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'choices':[{'message':{'content':'OK'}}]})
    resource = {'provider':'llm','api_key':'very-secret-offline-key','base_url':'https://fake.test/v1','model':'model-a'}
    result = await probe_resource(resource, transport=httpx.MockTransport(respond))
    assert result['ok'] is True
    assert len(requests) == 1
    assert requests[0].headers['authorization'] == 'Bearer very-secret-offline-key'
    assert 'very-secret' not in json.dumps(result)
    assert json.loads(requests[0].content)['model'] == 'model-a'


@pytest.mark.asyncio
async def test_probe_failure_redacts_key_and_does_not_report_available():
    from app.providers.probes import probe_resource
    async def respond(request):
        return httpx.Response(401, text='key very-secret-offline-key is invalid')
    result = await probe_resource({'provider':'fish','api_key':'very-secret-offline-key',
        'base_url':'https://fake.test','model':'s2.1-pro-free'}, transport=httpx.MockTransport(respond))
    assert result['ok'] is False
    assert result['status_code'] == 401
    assert 'very-secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_stt_probe_uses_audio_business_endpoint_not_project_permissions():
    from app.providers.probes import probe_resource
    async def respond(request):
        assert request.url.path == '/v1/audio/transcriptions'
        assert b'audio.wav' in request.content
        return httpx.Response(200,json={'text':''})
    result = await probe_resource({'provider':'siliconflow','api_key':'offline-key',
        'base_url':'https://fake.test/v1','model':'sensevoice'}, transport=httpx.MockTransport(respond))
    assert result['ok'] is True


@pytest.mark.asyncio
async def test_fish_probe_rejects_success_status_with_non_audio_body():
    from app.providers.probes import probe_resource
    async def respond(request):
        return httpx.Response(200,content=b'not audio' * 200,headers={'content-type':'application/json'})
    result = await probe_resource({'provider':'fish','api_key':'offline-key',
        'base_url':'https://fake.test','model':'free'}, transport=httpx.MockTransport(respond))
    assert not result['ok']
