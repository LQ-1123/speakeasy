"""管理端（控制面）：Sonder 运营控制台。与客户端完全解耦，仅管理身份可用。

三个路由出口，分工明确：
- ``router``：``/api/admin/*``，挂 ``require_admin``（鉴权 + CSRF + 审计）；
- ``api_public_router``：``/api/admin/session``、``/api/admin/logout``——
  **故意不鉴权**（前端要靠 session 判断是否跳登录页；退出必须幂等）；
- ``page_router``：``/admin`` 页面本身与登录/退出表单。
"""

from app.admin.router import api_public_router, page_router, router

__all__ = ["api_public_router", "page_router", "router"]
