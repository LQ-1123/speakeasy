"""Fish Audio TTS provider。

接口形态（v1）::

    POST {base}/v1/tts
    Authorization: Bearer <key>
    model: s2.1-pro          # 也接受放在 body 里
    {"text": "...", "reference_id": "...", "format": "wav"}

支持行内情绪标记：[excited] [laughing] [whispering] [break] —— 直接写进 text 即可。

并发模型（重要）
----------------
Fish 按**账号并发数**限流（Starter 档 5 路，超出直接回 429），而不是按每分钟请求数。
所有合成请求因此先过一道进程内的优先级闸门（:class:`_Limiter`）：

* 同时在途的请求不超过 ``SE_FISH_CONCURRENCY``（默认 5），多出来的**排队**而不是失败。
  以前并发一高就是 429 → 这条回复没有语音（严格音色模式下不允许换 Edge 兜底）；
* 排队分优先级：通话逐句 > 聊天回复 > 后台预热/音色校验。调用方用
  ``with fish_tts.priority("call"):`` 声明，不用层层传参；
* 连接复用：整个进程共用一个 ``httpx.AsyncClient``（keep-alive）。以前每次合成都新建
  客户端 = 每次都加载一遍 CA 证书 + 重新 TLS 握手。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
import logging
import math
import hashlib
import os
import random
import time
import weakref
from contextvars import ContextVar
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

#: 优先级（数字越小越先拿到并发名额）
_PRIORITY_LEVELS: dict[str, int] = {"call": 0, "chat": 1, "background": 2}
_priority_var: ContextVar[str] = ContextVar("fish_tts_priority", default="chat")

#: 各优先级默认最多排队多久（秒）；通话本身还有逐句 12 秒的总超时
_QUEUE_TIMEOUT_S: dict[str, float] = {"call": 10.0, "chat": 20.0, "background": 120.0}

#: Fish 认可的 mp3 码率（kbps）
_MP3_BITRATES = (64, 128, 192)
#: 上游拒绝 mp3_bitrate 参数时置 True：之后不再发送（回到 Fish 默认的 128kbps）
_bitrate_rejected = False
_pooled_bitrate_rejected: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def get_pool():
    from app.providers.runtime import get_pool as current_pool

    return current_pool()


class FishTTSError(RuntimeError):
    """Fish 调用失败。detail 里带原始响应，便于定位字段问题。"""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@contextlib.contextmanager
def priority(level: str) -> Iterator[None]:
    """声明这段代码里发起的合成属于哪一档：``call`` / ``chat`` / ``background``。"""
    token = _priority_var.set(level if level in _PRIORITY_LEVELS else "chat")
    try:
        yield
    finally:
        _priority_var.reset(token)


def concurrency_limit() -> int:
    """Fish 账号并发上限（``SE_FISH_CONCURRENCY``）。

    默认 5 = Starter 档（累计充值 < $100）。充值到 $100 是 15、$1000 是 50，升级后把这个
    环境变量调上去即可；设得比账号实际额度高只会换回 429。
    """
    try:
        value = int(os.getenv("SE_FISH_CONCURRENCY", "5"))
    except ValueError:
        value = 5
    return max(1, min(value, 200))


def queue_limit() -> int:
    """Maximum waiting syntheses; reject overload before opening HTTP requests."""
    try:
        return max(0, min(10000, int(os.getenv("SE_FISH_QUEUE_LIMIT", "20"))))
    except ValueError:
        return 20


def mp3_bitrate_kbps() -> int:
    """请求 Fish 输出的 mp3 码率（``SE_FISH_MP3_BITRATE``，默认 64）。

    语音回复是这台服务器出流量的大头；人声在 64kbps 下听感与 128kbps 几乎无差别，
    流量减半。想恢复 Fish 默认值设为 128。
    """
    if _bitrate_rejected:
        return 128
    try:
        value = int(os.getenv("SE_FISH_MP3_BITRATE", "64"))
    except ValueError:
        value = 64
    return value if value in _MP3_BITRATES else 64


def default_timeout_s() -> float:
    """单次合成的 HTTP 超时（``SE_FISH_TIMEOUT_S``，默认 60 秒）。

    并发名额只有几路，一个挂死的请求占着名额就是整体吞吐下降，所以不再用 120 秒。
    """
    try:
        value = float(os.getenv("SE_FISH_TIMEOUT_S", "60"))
    except ValueError:
        value = 60.0
    return max(5.0, min(value, 300.0))


def total_timeout_s() -> float:
    """One queue + synthesis + retry budget, independent of HTTP phase timeouts."""
    try:
        value = float(os.getenv("SE_FISH_TOTAL_TIMEOUT_S", "30"))
        return max(0.001, min(value, 300.0)) if math.isfinite(value) else 30.0
    except ValueError:
        return 30.0


# --------------------------------------------------------------------------- #
# 并发闸门（按优先级排队）
# --------------------------------------------------------------------------- #
class _Limiter:
    """最多 ``limit`` 个请求同时在途；其余按 (优先级, 先来后到) 排队。"""

    def __init__(self) -> None:
        self.active = 0
        self._waiting: dict[int, OrderedDict[asyncio.Future, None]] = {
            level: OrderedDict() for level in _PRIORITY_LEVELS.values()
        }
        self._waiting_count = 0
        self.stats = {"requests": 0, "queued": 0, "queue_timeouts": 0, "http_429": 0,
                      "peak_waiting": 0, "wait_ms_total": 0.0, "queue_full": 0,
                      "cancelled": 0}

    @property
    def waiting(self) -> int:
        return self._waiting_count

    async def acquire(self, level: int, timeout: float) -> None:
        self.stats["requests"] += 1
        if self.active < concurrency_limit() and not self.waiting:
            self.active += 1
            return
        if self.waiting >= queue_limit():
            self.stats["queue_full"] += 1
            raise FishTTSError("Fish synthesis queue full（合成排队已满）", status=429)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        level = level if level in self._waiting else 1
        queue = self._waiting[level]
        queue[future] = None
        self._waiting_count += 1
        self.stats["queued"] += 1
        self.stats["peak_waiting"] = max(self.stats["peak_waiting"], self.waiting)
        started = time.monotonic()
        timer = asyncio.get_running_loop().call_later(max(0.0, timeout), self._expire, future)
        try:
            await future
        except asyncio.CancelledError:
            self.stats["cancelled"] += 1
            # 被取消（通话打断 / 请求断开）：名额如果恰好已经发给我们，要还回去
            if future.done() and not future.cancelled() and future.exception() is None:
                self.release()
            raise
        finally:
            timer.cancel()
            if future in queue:
                del queue[future]
                self._waiting_count -= 1
            self.stats["wait_ms_total"] += (time.monotonic() - started) * 1000.0

    def _expire(self, future: asyncio.Future) -> None:
        if not future.done():
            self.stats["queue_timeouts"] += 1
            future.set_exception(FishTTSError("Fish 合成排队超时（并发名额已满）", status=429))

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        for level in sorted(self._waiting):
            queue = self._waiting[level]
            while queue and self.active < concurrency_limit():
                future, _ = queue.popitem(last=False)
                self._waiting_count -= 1
                if future.done():
                    continue
                self.active += 1
                future.set_result(None)


class _LoopState:
    __slots__ = ("limiter", "client", "models_cache", "__weakref__")

    def __init__(self) -> None:
        self.limiter = _Limiter()
        self.client: httpx.AsyncClient | None = None
        self.models_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()


#: 每个事件循环一份（生产只有一个；测试里每个用例一个，互不串状态）
_states: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState]" = weakref.WeakKeyDictionary()


def _state() -> _LoopState:
    loop = asyncio.get_running_loop()
    state = _states.get(loop)
    if state is None:
        state = _states[loop] = _LoopState()
    return state


def _client(state: _LoopState) -> httpx.AsyncClient:
    if state.client is None or state.client.is_closed:
        slots = concurrency_limit()
        state.client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=slots * 2 + 4, max_keepalive_connections=slots + 2,
                                keepalive_expiry=20.0),
        )
    return state.client


async def aclose() -> None:
    """关闭当前事件循环上的共享连接（应用停机时调用；不调用也只是连接随进程结束）。"""
    try:
        state = _states.get(asyncio.get_running_loop())
    except RuntimeError:
        return
    if state is not None and state.client is not None:
        client, state.client = state.client, None
        with contextlib.suppress(Exception):
            await client.aclose()


def limiter_stats() -> dict[str, Any]:
    """并发闸门的观测数据（/api/v3/health 的 voice.fish.queue）。"""
    try:
        state = _states.get(asyncio.get_running_loop())
    except RuntimeError:
        state = None
    limiter = state.limiter if state is not None else None
    stats = dict(limiter.stats) if limiter is not None else {}
    stats["wait_ms_total"] = round(float(stats.get("wait_ms_total", 0.0)), 1)
    return {"limit": concurrency_limit(), "queue_limit": queue_limit(), "active": limiter.active if limiter else 0,
            "waiting": limiter.waiting if limiter else 0, "mp3_bitrate": mp3_bitrate_kbps(), **stats}


# --------------------------------------------------------------------------- #
# mp3 时长估算
# --------------------------------------------------------------------------- #
_MP3_KBPS_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MP3_KBPS_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


def _mp3_frame_kbps(data: bytes) -> int:
    """读第一个 MPEG Layer III 帧头里的码率（kbps）；认不出来返回 0。"""
    view = bytes(data[:8192])
    start = 0
    if view[:3] == b"ID3" and len(view) >= 10:  # 跳过 ID3v2 标签（长度是 synchsafe 整数）
        start = 10 + ((view[6] & 0x7F) << 21 | (view[7] & 0x7F) << 14 | (view[8] & 0x7F) << 7 | (view[9] & 0x7F))
    for i in range(start, len(view) - 3):
        if view[i] != 0xFF or (view[i + 1] & 0xE0) != 0xE0:
            continue
        version, layer = (view[i + 1] >> 3) & 0x3, (view[i + 1] >> 1) & 0x3
        index, rate = (view[i + 2] >> 4) & 0xF, (view[i + 2] >> 2) & 0x3
        if version == 1 or layer != 1 or index in (0, 15) or rate == 3:
            continue
        return (_MP3_KBPS_V1 if version == 3 else _MP3_KBPS_V2)[index]
    return 0


def estimate_mp3_duration_ms(data: bytes) -> int:
    """按**实际**码率估算 mp3 时长（只用于气泡长度 / 进度条，不要求精确）。

    码率从音频自己的帧头读，读不到才退回请求时的配置值——这样无论上游是否接受了
    ``mp3_bitrate``、缓存里是不是旧码率的文件，气泡长度都不会差一倍。
    """
    if not data:
        return 0
    # Sum MPEG frame sample counts. This handles VBR and excludes large ID3
    # artwork without decoding audio or copying the encoded recording.
    start = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        start = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 |
                      (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
        if data[5] & 0x10:
            start += 10
    offset = start
    seconds = 0.0
    frames = 0
    while offset + 4 <= len(data):
        if data[offset] != 0xFF or data[offset + 1] & 0xE0 != 0xE0:
            if frames or offset - start >= 65536:
                break
            offset += 1
            continue
        version, layer = (data[offset + 1] >> 3) & 3, (data[offset + 1] >> 1) & 3
        index, rate = (data[offset + 2] >> 4) & 15, (data[offset + 2] >> 2) & 3
        if version == 1 or layer != 1 or index in (0, 15) or rate == 3:
            if frames:
                break
            offset += 1
            continue
        sample_rate = (44100, 48000, 32000)[rate] // (1 if version == 3 else 2 if version == 2 else 4)
        kbps = (_MP3_KBPS_V1 if version == 3 else _MP3_KBPS_V2)[index]
        padding = (data[offset + 2] >> 1) & 1
        frame_size = (144000 if version == 3 else 72000) * kbps // sample_rate + padding
        if offset + frame_size > len(data):
            break
        seconds += (1152 if version == 3 else 576) / sample_rate
        frames += 1
        offset += frame_size
    if frames:
        return round(seconds * 1000)
    kbps = _mp3_frame_kbps(data) or mp3_bitrate_kbps()
    return max(500, int(len(data) * 8 / (kbps * 1000) * 1000))


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
#: 复用的 keep-alive 连接可能刚好被对端关掉：这类错误换条新连接重试一次即可
_STALE_CONNECTION_ERRORS = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.CloseError)
#: 持有并发名额时遇到 429（账号在别处也有请求在跑）：退避重试这么多次
_RETRIES_ON_429 = 2


def _pool_client(lease: Any) -> httpx.AsyncClient:
    # Credentials belong to requests, never to cached client defaults.
    return httpx.AsyncClient(limits=httpx.Limits(
        max_connections=32, max_keepalive_connections=2, keepalive_expiry=20.0,
    ))


def _pooled_bitrate() -> int:
    try:
        value = int(os.getenv("SE_FISH_MP3_BITRATE", "64"))
    except ValueError:
        value = 64
    return value if value in _MP3_BITRATES else 64


@contextlib.asynccontextmanager
async def _pooled_lease(pool: Any, *, queue_budget: float, deadline: float, **kwargs):
    context = pool.lease("fish", deadline=deadline, **kwargs)
    # Keep queue-only timeout separate from the complete HTTP deadline. Enter
    # in this task so cancellation cannot strand a slot granted to a helper.
    async with asyncio.timeout(min(queue_budget, max(0.0, deadline - time.monotonic()))):
        lease = await context.__aenter__()
    try:
        yield lease
    except BaseException as exc:
        if not await context.__aexit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        await context.__aexit__(None, None, None)


async def _synthesize_pooled(
    pool: Any, text: str, *, model: str, reference_id: str, audio_format: str,
    timeout: float | None, queue_timeout: float | None,
) -> bytes:
    deadline = time.monotonic() + total_timeout_s()
    level = _priority_var.get()
    queue_budget = float(queue_timeout) if queue_timeout is not None else _QUEUE_TIMEOUT_S.get(level, 20.0)
    rejected = _pooled_bitrate_rejected.setdefault(pool, OrderedDict())
    excluded: set[str] = set()
    try:
        # Every actual HTTP attempt owns a fresh lease and budget reservation,
        # including a retry that only removes an unsupported format parameter.
        for attempt in range(2):
            async with _pooled_lease(
                pool, model=model, capability={"reference_id": reference_id.strip()} if reference_id.strip() else None,
                priority=level, deadline=deadline, queue_budget=queue_budget,
                exclude_resource_ids=excluded,
            ) as lease:
                identity = (lease.resource_id, lease.revision)
                payload: dict[str, object] = {
                    "text": text, "format": audio_format, "normalize": True, "latency": "normal",
                }
                if reference_id.strip():
                    payload["reference_id"] = reference_id.strip()
                if str(audio_format).lower() == "mp3" and identity not in rejected:
                    payload["mp3_bitrate"] = _pooled_bitrate()
                headers = {"Authorization": f"Bearer {lease.api_key}", "Content-Type": "application/json",
                           "Accept": "audio/*", "model": lease.model}
                async with pool.client(lease, _pool_client) as client:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        lease.failure(status=504, code="deadline")
                        raise FishTTSError("Fish total deadline exceeded", status=504)
                    attempt_timeout = min(float(timeout) if timeout else default_timeout_s(), remaining)
                    try:
                        response = await asyncio.wait_for(client.post(
                            (lease.base_url or "https://api.fish.audio").rstrip("/") + "/v1/tts",
                            json=payload, headers=headers,
                            timeout=httpx.Timeout(attempt_timeout, connect=min(attempt_timeout, 10.0)),
                        ), timeout=remaining)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        lease.failure(status=0, code=type(exc).__name__)
                        raise FishTTSError(f"Fish request failed: {type(exc).__name__}") from exc
                if response.status_code >= 400:
                    body = response.text[:600].replace(lease.api_key, "[redacted]")
                    detail = body.lower()
                    bitrate_denied = bool(response.status_code in (400, 422) and "mp3_bitrate" in payload
                                          and "mp3_bitrate" in detail
                                          and any(word in detail for word in (
                                              "unsupported", "not supported", "not allowed", "invalid",
                                              "unknown", "unexpected", "rejected", "must", "range",
                                              "不支持", "无效",
                                          )))
                    if bitrate_denied:
                        rejected[identity] = True
                        rejected.move_to_end(identity)
                        while len(rejected) > 1024:
                            rejected.popitem(last=False)
                    voice_denied = bool(response.status_code == 403 and reference_id.strip()
                                        and any(word in detail for word in ("reference", "voice"))
                                        and any(word in detail for word in ("permission", "denied", "forbidden", "access")))
                    lease.failure(status=response.status_code,
                                  code=f"voice_denied:{reference_id.strip()}" if voice_denied else
                                       "unsupported_bitrate" if bitrate_denied else "",
                                  retry_after=response.headers.get("Retry-After"))
                    if (response.status_code in (429, 503) or voice_denied or bitrate_denied) and attempt == 0:
                        if not bitrate_denied:
                            excluded.add(lease.resource_id)
                        continue
                    raise FishTTSError(f"Fish returned {response.status_code}", status=response.status_code,
                                       body=body)
                data = response.content
                if data:
                    lease.mark_output_started()
                if len(data) < 512:
                    lease.failure(status=0, code="invalid_audio")
                    raise FishTTSError("Fish returned invalid audio (too short)")
                lease.success()
                return data
    except asyncio.CancelledError:
        raise
    except FishTTSError:
        raise
    except TimeoutError as exc:
        raise FishTTSError("Fish queue deadline exceeded", status=504) from exc
    except Exception as exc:
        raise FishTTSError("Fish pool unavailable", status=int(getattr(exc, "status_code", 0) or 0)) from exc
    raise FishTTSError("Fish pool unavailable", status=503)


async def synthesize(
    text: str,
    *,
    api_key: str,
    base_url: str = "https://api.fish.audio",
    model: str = "s2.1-pro",
    reference_id: str = "",
    audio_format: str = "wav",
    timeout: float | None = None,
    queue_timeout: float | None = None,
) -> bytes:
    """合成并返回音频字节。失败抛 FishTTSError（带原始 body）。

    优先级取自 :func:`priority` 上下文（默认 ``chat``）。``timeout`` 是 HTTP 超时，
    ``queue_timeout`` 是等并发名额的最长时间；排队超时按 429 抛出（调用方走既有的降级路径）。
    """
    global _bitrate_rejected

    pool = get_pool()
    if pool is not None and pool.applies("fish", model):
        from app.jobs import wait_for_background_capacity

        await wait_for_background_capacity()
        return await _synthesize_pooled(pool, text, model=model, reference_id=reference_id,
                                        audio_format=audio_format, timeout=timeout, queue_timeout=queue_timeout)

    from app.jobs import wait_for_background_capacity  # local: optional maintenance context

    await wait_for_background_capacity()

    key = (api_key or "").strip()
    if not key:
        raise FishTTSError("未配置 Fish API Key")

    url = (base_url or "https://api.fish.audio").rstrip("/") + "/v1/tts"
    payload: dict[str, object] = {
        "text": text,
        "format": audio_format,
        "normalize": True,
        "latency": "normal",
    }
    if reference_id.strip():
        payload["reference_id"] = reference_id.strip()
    if str(audio_format).lower() == "mp3" and not _bitrate_rejected:
        payload["mp3_bitrate"] = mp3_bitrate_kbps()
    dropped_bitrate = False

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "audio/*",
    }
    if model.strip():
        headers["model"] = model.strip()

    level_name = _priority_var.get()
    http_timeout = float(timeout) if timeout else default_timeout_s()
    deadline = time.monotonic() + total_timeout_s()
    state = _state()
    # Acquire directly: handing the slot to a helper task creates an ownership
    # gap if the parent is cancelled immediately after that helper returns.
    queue_budget = float(queue_timeout) if queue_timeout is not None else _QUEUE_TIMEOUT_S.get(level_name, 20.0)
    await state.limiter.acquire(
        _PRIORITY_LEVELS.get(level_name, 1),
        min(queue_budget, max(0.0, deadline - time.monotonic())),
    )
    acquired = True
    try:
        stale_retry = True
        busy_retries = _RETRIES_ON_429
        repeated = False
        while True:
            if repeated:
                # Return quota before a low-priority retry waits for foreground.
                state.limiter.release()
                acquired = False
                try:
                    await asyncio.wait_for(wait_for_background_capacity(),
                                           timeout=max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError as exc:
                    raise FishTTSError("Fish total deadline exceeded（合成超时）", status=504) from exc
                await state.limiter.acquire(_PRIORITY_LEVELS.get(level_name, 1),
                                            min(queue_budget, max(0.0, deadline - time.monotonic())))
                acquired = True
            repeated = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FishTTSError("Fish total deadline exceeded（合成超时）", status=504)
            attempt_timeout = min(http_timeout, remaining)
            request_timeout = httpx.Timeout(attempt_timeout, connect=min(attempt_timeout, 10.0))
            try:
                resp = await asyncio.wait_for(
                    _client(state).post(url, json=payload, headers=headers, timeout=request_timeout),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                raise FishTTSError("Fish total deadline exceeded（合成超时）", status=504) from exc
            except _STALE_CONNECTION_ERRORS as exc:
                if stale_retry:
                    stale_retry = False
                    continue
                raise FishTTSError(f"连接 Fish 失败: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise FishTTSError(f"连接 Fish 失败: {exc}") from exc

            if resp.status_code == 429 and busy_retries > 0:
                state.limiter.stats["http_429"] += 1
                attempt = _RETRIES_ON_429 - busy_retries
                busy_retries -= 1
                await asyncio.sleep(min(0.4 * (2 ** attempt) + random.uniform(0.0, 0.2),
                                        max(0.0, deadline - time.monotonic())))
                continue
            if resp.status_code in (400, 422) and "mp3_bitrate" in payload:
                # 也许是这个账号 / 模型不认码率参数：去掉它重发一次
                payload.pop("mp3_bitrate", None)
                dropped_bitrate = True
                continue
            break
    finally:
        if acquired:
            state.limiter.release()

    if dropped_bitrate and resp.status_code < 400:
        # 去掉参数就成功了，说明确实是参数的问题：本进程内不再发送（回到 Fish 默认 128kbps）。
        # 去掉后仍然失败则是别的原因（音色失效等），不能因此永久放弃省流量的码率。
        _bitrate_rejected = True
        log.warning("Fish 不接受 mp3_bitrate 参数，已回退到默认码率")

    if resp.status_code >= 400:
        if resp.status_code == 429:
            state.limiter.stats["http_429"] += 1
        body = resp.text[:600]
        raise FishTTSError(
            f"Fish 返回 {resp.status_code}", status=resp.status_code, body=body
        )
    data = resp.content
    if len(data) < 512:
        raise FishTTSError("Fish 返回的音频数据异常（过短）", body=data[:200].decode("utf-8", "ignore"))
    return data


def _catalog_output(response: httpx.Response) -> list[dict]:
    try:
        payload = response.json() or {}
        return [{"id": str(row.get("_id") or row.get("id") or ""),
                 "title": str(row.get("title") or row.get("name") or ""),
                 "description": str(row.get("description") or "")[:140],
                 "languages": row.get("languages") or [], "tags": (row.get("tags") or [])[:6],
                 "cover": str(row.get("cover_image") or ""), "total": int(payload.get("total") or 0)}
                for row in payload.get("items") or [] if isinstance(row, dict)]
    except (ValueError, AttributeError, TypeError) as exc:
        raise FishTTSError("Fish voice catalog response is invalid") from exc


async def _list_models_pooled(pool: Any, *, model: str, page_number: int, page_size: int,
                              use_cache: bool, timeout: float) -> list[dict]:
    state = _state()
    try:
        async with pool.lease("fish", model, priority=_priority_var.get(),
                              deadline=time.monotonic() + timeout) as lease:
            cache_key = ("pool", lease.resource_id, lease.revision, int(page_number), int(page_size))
            hit = state.models_cache.get(cache_key) if use_cache else None
            if hit and time.monotonic() - hit[0] < 600:
                state.models_cache.move_to_end(cache_key)
                lease.success()
                return [dict(item) for item in hit[1]]
            try:
                async with pool.client(lease, _pool_client) as client:
                    response = await asyncio.wait_for(client.get(
                        (lease.base_url or "https://api.fish.audio").rstrip("/") + "/model",
                        headers={"Authorization": f"Bearer {lease.api_key}"},
                        params={"page_size": int(page_size), "page_number": int(page_number)},
                        timeout=timeout,
                    ), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lease.failure(status=0, code=type(exc).__name__)
                raise FishTTSError(f"Fish catalog request failed: {type(exc).__name__}") from exc
            if response.status_code >= 400:
                lease.failure(status=response.status_code, retry_after=response.headers.get("Retry-After"))
                raise FishTTSError(f"Fish returned {response.status_code}", status=response.status_code,
                                   body=response.text[:400].replace(lease.api_key, "[redacted]"))
            try:
                output = _catalog_output(response)
            except FishTTSError:
                lease.failure(status=0, code="invalid_response")
                raise
            lease.success()
            if use_cache:
                state.models_cache[cache_key] = (time.monotonic(), output)
                state.models_cache.move_to_end(cache_key)
                while len(state.models_cache) > 128:
                    state.models_cache.popitem(last=False)
            return [dict(item) for item in output]
    except asyncio.CancelledError:
        raise
    except FishTTSError:
        raise
    except Exception as exc:
        raise FishTTSError("Fish catalog pool unavailable", status=int(getattr(exc, "status_code", 0) or 0)) from exc


async def list_models(
    *, api_key: str, base_url: str = "https://api.fish.audio",
    page_number: int = 1, page_size: int = 100, use_cache: bool = True,
    timeout: float = 30.0, model: str = "s2.1-pro",
) -> list[dict]:
    """List official/custom voices using the bounded shared connection pool."""
    from app.jobs import wait_for_background_capacity

    pool = get_pool()
    if pool is not None and pool.applies("fish", model):
        await wait_for_background_capacity()
        return await _list_models_pooled(pool, model=model, page_number=page_number, page_size=page_size,
                                         use_cache=use_cache, timeout=timeout)

    key = (api_key or "").strip()
    if not key:
        raise FishTTSError("未配置 Fish API Key")
    state = _state()
    base_url = (base_url or "https://api.fish.audio").rstrip("/")
    cache_key = (base_url, int(page_number), int(page_size), hashlib.sha256(key.encode()).digest())
    hit = state.models_cache.get(cache_key) if use_cache else None
    if hit and time.monotonic() - hit[0] < 600:
        state.models_cache.move_to_end(cache_key)
        return [dict(item) for item in hit[1]]
    await wait_for_background_capacity()
    level = _priority_var.get()
    await state.limiter.acquire(_PRIORITY_LEVELS.get(level, 1), min(timeout, _QUEUE_TIMEOUT_S.get(level, 20)))
    try:
        response = await asyncio.wait_for(_client(state).get(
            base_url + "/model", headers={"Authorization": f"Bearer {key}"},
            params={"page_size": int(page_size), "page_number": int(page_number)}, timeout=timeout), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise FishTTSError("Fish voice catalog timeout", status=504) from exc
    except httpx.HTTPError as exc:
        raise FishTTSError(f"连接 Fish 失败: {exc}") from exc
    finally:
        state.limiter.release()
    if response.status_code >= 400:
        raise FishTTSError(f"Fish 返回 {response.status_code}", status=response.status_code, body=response.text[:400])
    output = _catalog_output(response)
    if use_cache:
        state.models_cache[cache_key] = (time.monotonic(), output)
        state.models_cache.move_to_end(cache_key)
        while len(state.models_cache) > 128:
            state.models_cache.popitem(last=False)
    return [dict(item) for item in output]


async def pick_voice_for(
    *, language: str = "en", gender: str = "", pages: int = 3,
    api_key: str = "", base_url: str = "https://api.fish.audio",
    timeout: float = 30.0, used: set[str] | None = None, model: str = 's2.1-pro',
) -> str:
    """Pick an unused voice, preserving gender before relaxing language."""
    pool: list[dict] = []
    try:
        for page in range(1, max(1, pages) + 1):
            rows = await list_models(api_key=api_key, base_url=base_url,
                                     page_number=page, page_size=100, timeout=timeout, model=model)
            pool.extend(rows)
            if len(rows) < 100:
                break
    except Exception as exc:  # noqa: BLE001 - preserve catalog fallback contract
        log.warning("获取 Fish 音色失败（用默认音色）: %s", exc)
        return ""
    language, gender = (language or "").strip().lower(), (gender or "").strip().lower()
    for need_language, need_gender in ((True, True), (False, True), (False, False)):
        candidates = [row["id"] for row in pool if row.get("id") and row["id"] not in (used or set())
                      and (not need_language or not language or language in [str(x).lower() for x in row.get("languages") or []])
                      and (not need_gender or not gender or gender in [str(x).lower() for x in row.get("tags") or []])]
        if candidates:
            return random.choice(candidates)
    return ""
