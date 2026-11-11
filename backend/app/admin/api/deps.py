"""管理 API 的公共依赖：守卫与 ``app.state`` 访问器。

为什么单独一个模块
------------------
拆分前这些辅助函数住在 ``app/admin/router.py``，由每个 handler 直接调用。
拆成按域的子路由后，``router.py`` 只剩 ``include_router``，子模块再各自 import
``router.py`` 会形成循环导入，所以把**跨域共用**的东西收在这里。

:func:`guard` 是过渡态
----------------------
P1 的鉴权任务（task-1）已在 ``app/admin/auth.py`` 落地 ``require_admin`` 依赖
（鉴权 + CSRF + 操作日志一把抓）。拆分任务的约束是"**旧路径与行为完全不变**"，
所以迁移过来的 handler 继续显式调 :func:`guard`（等价于旧 ``_guard``），
没有改成 ``dependencies=[Depends(require_admin)]``。

**CSRF 采用"灰名单"策略**（P1 收口时确定）
-------------------------------------------
直接挂 ``require_admin`` 会让旧管理台（``page.py`` 内联 JS）在 Cookie 登录下
所有写操作 403——旧台不带 ``X-Admin-CSRF`` 头。而完全不校验又会让**新** React
管理台失去 CSRF 保护。两者都不可接受。

因此 :func:`guard` 对写请求这样做：

* 请求**带了** ``X-Admin-CSRF`` 头 → 必须校验通过（值错/过期/来源非法一律 403）。
  新 React 台每次写都会带，于是它全程受保护。
* 请求**没带**该头 → 放行（旧台与脚本的既有行为不变），
  但来源若是 ``cross-site`` 仍拒绝。

这样 CSRF 是"**带就必验、不带则维持旧行为**"：新界面立刻获得保护，
旧界面照常可用，第 4 期删掉 ``page.py`` 后再把 ``guard`` 整体换成
``require_admin`` 即可收口。

:func:`guard` 现在直接委托 ``auth`` 里已有的判定，不重复实现分支逻辑，
保证过渡期与正式依赖的放行规则永远一致。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from app.admin import auth as admin_auth

#: 回环地址白名单（未配口令时本机直通）。
#: 权威定义在 ``app.admin.auth.LOOPBACK_HOSTS``，这里只是别名，避免两处漂移。
LOOPBACK = admin_auth.LOOPBACK_HOSTS


# 守卫去哪了
# ----------
# P1~P3 期间这里有一个 ``guard(request)``：显式鉴权 + 「灰名单」CSRF（带 CSRF 头才校验），
# 由每个 handler 自己调用。那是为了在**不破坏旧管理台**的前提下让新控制台先受保护。
#
# P4 删除旧管理台后不再需要兼容，于是收口成路由级依赖
# ``app.admin.auth.require_admin``（挂在 ``app/admin/router.py`` 的 ``router`` 上）：
#   - 鉴权：Cookie 会话 → ``X-Admin-Token`` → 未配口令时本机直通
#   - CSRF：Cookie 身份的写请求**一律**要求同源 + ``X-Admin-CSRF``（token/本机豁免）
#   - 审计：写操作成功后自动落 ``admin_audit``
#
# 所以本模块只剩「取运行时对象」的访问器。**不要**再往 handler 里加显式鉴权，
# 那会变成两套判定并存——P4 之前就吃过这个亏（灰名单与全强制两套规则漂移）。


def db(request: Request) -> Any:
    raise HTTPException(status_code=403, detail="请先登录管理台")


def db(request: Request) -> Any:
    """数据库连接；未就绪返回 ``None``（调用方按各自格式降级，不抛异常）。"""
    return getattr(getattr(request.app.state, "v2", None), "db", None)


def services(request: Request) -> Any:
    """V2 服务容器（引擎挂在它下面）；未初始化抛 503。"""
    value = getattr(request.app.state, "v2", None)
    if value is None:
        raise HTTPException(status_code=503, detail="服务未初始化")
    return value


def studio(request: Request) -> tuple[Any, Any, Any, Any]:
    """角色工坊的四个运行期对象：``(persona_studio, db, service, store)``。

    与原 ``router.py::_studio`` 一致——``persona_studio`` 延迟导入，
    避免管理端未使用角色工坊时也把整包素材/LLM 依赖拉起来。
    """
    from app.admin import persona_studio  # noqa: PLC0415

    database = getattr(getattr(request.app.state, "v2", None), "db", None)
    service = getattr(request.app.state, "v3", None)
    store = getattr(request.app.state, "conversation_store", None)
    return persona_studio, database, service, store


def queue(request: Request) -> Any:
    """持久化后台任务队列；未装配返回 ``None``。"""
    return getattr(request.app.state, "background_jobs", None)


__all__ = ["LOOPBACK", "db", "queue", "services", "studio"]
