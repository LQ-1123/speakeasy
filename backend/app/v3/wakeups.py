"""用户级唤醒：把「这个用户有新事件 / 会话状态变了」通知给挂着的长连接。

为什么需要
----------
事件长连接（``/api/v3/client/events``）原先每秒醒一次：查一遍会话是否有效、再查一遍
有没有新事件。每个在线用户每秒固定 2 次线程切换 + 数条 SQL，100 人在线就是每秒
200 次线程切换——用户什么都没做，服务器已经在空转。

现在连接平时睡着，只在三种情况下醒来：

1. 事件收集器（:func:`app.v3.client_events.collect_events`）给这个用户写了新事件；
2. 这个用户的会话被撤销 / 账号被归档（必须立刻复核，不能等兜底轮询）；
3. 兜底定时到点（防止任何一条通知丢失，见 ``client_api.EVENT_POLL_FALLBACK_S``）。

本模块刻意零依赖（不 import 任何业务模块），数据层、账号层都可以安全引用。
``notify`` 线程安全：收集器跑在工作线程里，等待方在事件循环里。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Iterable


class Waiter:
    """一条长连接的唤醒句柄（绑定创建它的事件循环）。"""

    __slots__ = ("loop", "event")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.event = asyncio.Event()


_lock = threading.Lock()
_waiters: dict[str, set[Waiter]] = {}


def subscribe(user_id: str) -> Waiter:
    """登记一个等待者；必须在事件循环里调用，用完务必 :func:`unsubscribe`。"""
    waiter = Waiter(asyncio.get_running_loop())
    with _lock:
        _waiters.setdefault(str(user_id), set()).add(waiter)
    return waiter


def unsubscribe(user_id: str, waiter: Waiter) -> None:
    with _lock:
        group = _waiters.get(str(user_id))
        if group is None:
            return
        group.discard(waiter)
        if not group:
            _waiters.pop(str(user_id), None)


def notify(user_ids: Iterable[str]) -> int:
    """唤醒这些用户的全部等待者，返回唤醒的连接数。任何线程都可以调用。"""
    targets: list[Waiter] = []
    with _lock:
        for user_id in user_ids:
            targets.extend(_waiters.get(str(user_id), ()))
    woken = 0
    for waiter in targets:
        try:
            waiter.loop.call_soon_threadsafe(waiter.event.set)
            woken += 1
        except RuntimeError:  # 事件循环已关闭（停机 / 测试收尾）：没有人需要被唤醒了
            continue
    return woken


def waiting_users() -> int:
    """当前挂着长连接的用户数（观测用）。"""
    with _lock:
        return len(_waiters)


__all__ = ["Waiter", "notify", "subscribe", "unsubscribe", "waiting_users"]
