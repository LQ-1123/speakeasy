"""「真实用户」与「空注册记录」的口径（管理台唯一真相）。

为什么需要这个模块
------------------
客户端首次启动会用随机 ``device_key`` 换一个 ``user_id``
（``frontend/src/w4/v3/api.ts``），正常情况下同一台设备的身份是稳定的。
但只要 localStorage 是空的——无痕窗口、新浏览器 profile、重装 App、
清应用数据——就会**再造一个用户**。

于是 ``v3_users`` 的行数很快就不等于人数。线上实测：91 行里 75 行没有任何
好友、消息、动态、设备注册，是纯粹的注册残留；真正注册过推送设备的只有 1 行。

如果总览直接把 ``COUNT(*)`` 当"用户总数"，任何人都读成"有 91 个人在用"。
所以口径必须显式拆开，并且**只在这里定义一次**——总览与用户列表各写一套判定，
迟早会漂移成两个数字打自己脸。

三个口径
--------
``total``
    ``v3_users`` 全部行数（原始注册数）。保留它是因为排查"为什么行数涨了"时
    要看得到原始值。
``real``
    **有任何使用痕迹**的用户：注册过推送设备 / 有好友关系 / 发过动态 /
    有聊天消息 / 发过 moment。满足任一即是。
    注意：这不是"真人"的严格定义，只是"这个身份被用过"的更好代理。
``empty``
    ``total - real``：没有上述业务信号的身份。它不能作为删除依据：
    已验证邮箱账号、资料、收藏和媒体可能不在这些旧信号中。账号模式采用显式归档。

``registered``
    单独给出"注册过推送设备"的数量。它比 ``real`` 更严格，是"装过 App 并且
    授权过通知"的人数，通常最接近运营心里的"用户数"，所以单独暴露而不是塞进 real。

为什么不写成一条 SQL
--------------------
痕迹分散在 5 张表里，其中 ``user_moments`` 在极老库里可能不存在。一条大 SQL 里
只要有一张表缺失就会整体报错，而总览是管理台落地页——500 就整页不可用
（``overview.py`` 模块文档里有同样的容错原则）。这里改成逐表查询 + 单表容错，
缺表只是少一个信号，不会拖垮统计。
"""

from __future__ import annotations

import sqlite3
import json
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 每个信号一条独立查询（单表失败只少一个信号，不影响其它）。
#: 键名只用于日志，顺序无关。
SIGNAL_QUERIES: dict[str, str] = {
    # 装过 App 并注册了推送——最硬的"真的在用"信号
    "devices": "SELECT DISTINCT user_id FROM client_devices WHERE user_id IS NOT NULL",
    # 好友关系（AI 主动加好友也会写这张表）
    "friendships": "SELECT DISTINCT user_id FROM friendships WHERE user_id IS NOT NULL",
    # 用户自己的动态
    "posts": "SELECT DISTINCT user_id FROM user_posts WHERE user_id IS NOT NULL",
    # 用户发的 moment（极老库可能没这张表）
    "moments": "SELECT DISTINCT user_id FROM user_moments WHERE user_id IS NOT NULL",
    # 聊天消息：messages 挂 friend_id，friends.owner_user_id 才是用户
    "messages": (
        "SELECT DISTINCT f.owner_user_id AS user_id"
        " FROM friends f JOIN messages m ON m.friend_id = f.id"
        " WHERE f.owner_user_id IS NOT NULL"
    ),
}

#: "注册过推送设备"的单独查询（``registered`` 口径）。
REGISTERED_SQL = "SELECT DISTINCT user_id FROM client_devices WHERE user_id IS NOT NULL"


def _ids(db: Any, sql: str) -> set[str]:
    """执行一条 ``SELECT user_id`` 查询并收集 id；任何失败都返回空集。

    容错是刻意的：``user_moments`` 在老库不存在、``client_devices`` 在从未
    注册过推送的库里可能为空，这些都不该让管理台统计失败。
    """
    if db is None:
        return set()
    try:
        with db._lock:  # noqa: SLF001 - 与 DB 层共用同一把锁
            rows = db.conn.execute(sql).fetchall()
    except (sqlite3.Error, RuntimeError, AttributeError) as exc:
        log.debug("user_scope 查询失败（忽略）: %s", exc)
        return set()
    out: set[str] = set()
    for row in rows:
        try:
            value = str(row[0] or "").strip()
        except (IndexError, TypeError):
            continue
        if value:
            out.add(value)
    return out


def signal_ids(db: Any) -> dict[str, set[str]]:
    """逐信号返回用户 id 集合（便于排查"这条算不算真实用户"）。"""
    return {name: _ids(db, sql) for name, sql in SIGNAL_QUERIES.items()}


def real_ids(db: Any) -> set[str]:
    """有任何使用痕迹的用户 id 集合。"""
    merged: set[str] = set()
    for ids in signal_ids(db).values():
        merged |= ids
    return merged


def registered_ids(db: Any) -> set[str]:
    """注册过推送设备的用户 id 集合（比 :func:`real_ids` 更严格）。"""
    return _ids(db, REGISTERED_SQL)


def counts(db: Any) -> dict[str, int]:
    """总览用的口径汇总。

    返回 ``{total, real, empty, registered}``。

    **必须与 ``v3_users`` 求交集**。痕迹表里可能残留指向已不存在用户的 id：
    线上实测有 ``messages`` → ``friends.owner_user_id = 'local'``——那是 V2 单用户
    时代的哨兵值，不是任何 ``v3_users`` 行。不取交集的话 ``real`` 会变成 18 而
    ``total`` 只有 17，界面上就出现"有使用痕迹 18 个、合计 17 条"这种自己打脸的
    数字（这是实际发生过的缺陷，不是假想）。

    ``empty`` 用 ``total - real``，并夹到 0：两者来自不同时刻的查询。
    """
    stats = {"total": 0, "real": 0, "empty": 0, "registered": 0}
    if db is None:
        return stats
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute("SELECT id FROM v3_users").fetchall()
    except (sqlite3.Error, RuntimeError, AttributeError) as exc:
        log.debug("user_scope: v3_users 读取失败（忽略）: %s", exc)
        return stats

    existing: set[str] = set()
    for row in rows:
        try:
            value = str(row[0] or "").strip()
        except (IndexError, TypeError):
            continue
        if value:
            existing.add(value)

    stats["total"] = len(existing)
    real = real_ids(db) & existing
    stats["real"] = len(real)
    stats["empty"] = max(0, len(existing) - len(real))
    stats["registered"] = len(registered_ids(db) & existing)
    return stats


#: 用户列表 ``scope`` 参数的合法取值。
SCOPES: tuple[str, ...] = ("formal", "history", "all", "real", "empty")


def formal_ids(db: Any) -> set[str]:
    return _ids(db, "SELECT u.id FROM v3_users u JOIN v3_accounts a ON a.user_id=u.id"
                " WHERE u.archived_at='' AND a.email_verified_at<>''")


def account_counts(db: Any) -> dict[str, int]:
    """Stable account, session and enabled push-device counts, never people."""
    existing = _ids(db, "SELECT id FROM v3_users")
    formal = formal_ids(db)
    result = {"formal": len(formal), "history": len(existing - formal), "sessions": 0, "devices": 0}
    for key, sql in {
        "sessions": "SELECT COUNT(*) FROM v3_sessions s JOIN v3_accounts a ON a.user_id=s.user_id"
                    " JOIN v3_users u ON u.id=s.user_id WHERE s.revoked_at=''"
                    " AND datetime(s.expires_at)>datetime('now') AND a.status='active'"
                    " AND a.email_verified_at<>'' AND u.archived_at=''",
        "devices": "SELECT COUNT(*) FROM client_devices d JOIN v3_accounts a ON a.user_id=d.user_id"
                   " JOIN v3_users u ON u.id=d.user_id WHERE d.enabled=1 AND u.archived_at=''"
                   " AND a.status='active' AND a.email_verified_at<>''",
    }.items():
        try:
            with db._lock:
                result[key] = int(db.conn.execute(sql).fetchone()[0])
        except (sqlite3.Error, AttributeError, RuntimeError):
            pass
    return result


def email_map(db: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if db is None:
        return result
    for sql, archived in (("SELECT user_id,email FROM v3_accounts", False),
                          ("SELECT user_id,account_json FROM v3_identity_archives", True)):
        try:
            with db._lock:
                for row in db.conn.execute(sql):
                    result[str(row[0])] = str(json.loads(row[1]).get("email", "") if archived else row[1])
        except (sqlite3.Error, AttributeError, ValueError):
            pass
    return result


__all__ = ["REGISTERED_SQL", "SCOPES", "SIGNAL_QUERIES", "counts", "real_ids", "registered_ids", "signal_ids"]
