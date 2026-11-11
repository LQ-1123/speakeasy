"""管理 API（``/api/admin``）按域拆分的子路由包。

约定
----
1. **前缀只在两处出现**：``app/admin/router.py`` 的父路由持 ``prefix="/api/admin"``，
   各子模块只声明自己的域（如 ``APIRouter(prefix="/media")``），最终路径由两层拼接。
   因此子模块里的路径永远写相对域的短路径（``/stats`` 而不是 ``/api/admin/media/stats``）。
2. **本包不 import** ``app.admin.router``。父路由负责 ``include_router``；反向引用会形成
   循环导入（``router.py`` → ``api/xxx.py`` → ``router.py``）。跨域共用的卫兵与
   ``app.state`` 访问器放在 :mod:`app.admin.api.deps`。
3. **旧路径与行为不变**：从 ``router.py`` 迁入的 handler 一律保持函数名、签名、
   返回结构与错误语义（含返回 ``{"ok": False, "issues": [...]}`` 配 200 的历史写法），
   新接口才按 ``docs/ADMIN_CONSOLE_CONTRACT.md`` §1.3 的 422 约定。

模块分布
--------
===================  ===============================================  ==============
模块                 覆盖路径                                          来源
===================  ===============================================  ==============
``overview.py``      ``GET /overview``                                新增（P1）
``session.py``       ``GET /session``、``POST /logout``               新增（P1）
``jobs.py``          ``GET /jobs``、``GET /jobs/kinds``、             新增（P1）
                     ``POST /jobs/{kind}/run``
``media.py``         ``/media/*``                                     旧路由迁移
``personas.py``      ``/personas/*``、``/preview-personas``、          旧路由迁移
                     ``/voice-pool/*``
``prompts.py``       ``/prompt/*``                                    旧路由迁移
``lexicons.py``      ``/lexicons/*``、``/status``、``/usage``、        旧路由迁移
                     ``/simulate``
``config.py``        ``/config/*``                                    旧路由迁移
``release.py``       ``/app-release/*``                               旧路由迁移
``users.py``         用户管理（P3）                                    空模块
``events.py``        事件公共池（P3）                                  空模块
``posts.py``         待发动态队列（P3）                                空模块
``audit.py``         操作日志查询（P1 鉴权任务落地 admin_audit 后接）   空模块
===================  ===============================================  ==============
"""

from __future__ import annotations

__all__: list[str] = []
