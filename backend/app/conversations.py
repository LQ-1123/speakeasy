"""对话持久化：每个角色一份，落盘到 ``data/conversations.json``。

设计
----
- **按键分区**：``{persona_id: {"updated_at":..., "messages":[...]}}``
- **消息是 UI 可见序列**，不是 LLM 的 prompt 历史：每条只有
  ``{role, text, ts, image?}``，足够重建微信气泡。
- **图片单独存文件**：data URL 解码后写到 ``data/uploads/<sha1>.<ext>``，
  消息里只留 ``/api/uploads/<sha1>.<ext>``，避免 JSON 被 base64 撑爆。
- **原子写**：先写 ``.tmp`` 再 ``os.replace``，避免断电留半截文件。
- **封顶**：每个角色最多保留 ``max_messages`` 条，超出丢最早的。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

_DATA_URL = re.compile(r"^data:image/(?P<kind>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.S)
_EXT = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}


class ConversationStore:
    """按角色存储对话。线程安全（WS 在处理线程里调用）。"""

    def __init__(self, data_dir: str, *, max_messages: int = 400) -> None:
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "conversations.json")
        self.upload_dir = os.path.join(data_dir, "uploads")
        self.max_messages = max_messages
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 读写文件
    def _load_all(self) -> dict[str, dict[str, Any]]:
        # 每次都读盘：文件很小（<1MB）。常驻缓存会在多进程或外部修改场景下
        # 读到旧快照、并把旧快照整体写回，直接覆盖别人的数据。
        data: dict[str, dict[str, Any]] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
            except Exception as exc:  # noqa: BLE001 - 文件坏了不能让服务起不来
                log.warning("读取 conversations.json 失败（按空处理）: %s", exc)
        return data

    def _flush(self, data: dict[str, dict[str, Any]]) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".conv-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ 图片
    def save_image(self, data_url: str) -> str | None:
        """把 data URL 存成文件，返回可访问的 URL 路径。"""
        m = _DATA_URL.match(data_url or "")
        if not m:
            return None
        try:
            blob = base64.b64decode(m.group("data"), validate=False)
        except Exception:  # noqa: BLE001
            return None
        if not blob:
            return None
        digest = hashlib.sha1(blob).hexdigest()[:20]
        ext = _EXT.get(m.group("kind").lower(), "png")
        name = f"{digest}.{ext}"
        os.makedirs(self.upload_dir, exist_ok=True)
        target = os.path.join(self.upload_dir, name)
        if not os.path.exists(target):
            with open(target, "wb") as f:
                f.write(blob)
        return f"/api/uploads/{name}"

    # ------------------------------------------------------------ 对外 API
    def load(self, persona_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conv = self._load_all().get(persona_id) or {}
            msgs = conv.get("messages")
            return list(msgs) if isinstance(msgs, list) else []

    def append(self, persona_id: str, message: dict[str, Any]) -> None:
        """追加一条消息并立即落盘。``message`` 至少要有 role/text。"""
        text = (message.get("text") or "").strip()
        image = message.get("image")
        if not text and not image:
            return
        entry: dict[str, Any] = {
            "role": message.get("role") or "user",
            "text": text,
            "ts": message.get("ts") or time.time(),
        }
        if image:
            entry["image"] = image
        with self._lock:
            data = self._load_all()
            conv = data.setdefault(persona_id, {"messages": []})
            msgs = conv.setdefault("messages", [])
            msgs.append(entry)
            if len(msgs) > self.max_messages:
                del msgs[: len(msgs) - self.max_messages]
            conv["updated_at"] = time.time()
            data[persona_id] = conv
            self._flush(data)

    def clear(self, persona_id: str) -> bool:
        with self._lock:
            data = self._load_all()
            existed = persona_id in data
            if existed:
                del data[persona_id]
                self._flush(data)
            return existed

    def summary(self) -> list[dict[str, Any]]:
        """每个角色的元信息（给侧栏显示预览用）。"""
        with self._lock:
            out = []
            for pid, conv in self._load_all().items():
                msgs = conv.get("messages") or []
                last = msgs[-1] if msgs else None
                out.append(
                    {
                        "persona_id": pid,
                        "count": len(msgs),
                        "updated_at": conv.get("updated_at"),
                        "last_text": (last or {}).get("text", ""),
                        "last_role": (last or {}).get("role"),
                    }
                )
            return out
