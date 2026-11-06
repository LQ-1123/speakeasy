"""SpeakEasy V3：以 AI Persona 为中心的社交发现层。

模块
----
- :mod:`app.v3.identity`      当前用户上下文（contextvar）
- :mod:`app.v3.security`      设备身份注册 / 请求鉴权 / 依赖
- :mod:`app.v3.schema`        V3 表结构 + Persona 语料入库
- :mod:`app.v3.seed_data`     Persona 池与预生成动态
- :mod:`app.v3.social_engine` 事件概率/匹配/冷却（可注入随机源）
- :mod:`app.v3.service`       业务服务（信息流、好友、动态、事件）
- :mod:`app.v3.router`        ``/api/v3`` REST 接口
"""

from __future__ import annotations

__all__ = ["VERSION"]

VERSION = "3.0.0"
