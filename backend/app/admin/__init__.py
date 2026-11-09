"""管理端（控制面）：词库管理。与客户端完全解耦，仅本机可用。"""

from app.admin.router import page_router, router

__all__ = ["page_router", "router"]
