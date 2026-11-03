"""图片/媒体存储（用户上传的照片）。

- 图片落到 ``data/uploads``，返回安全文件名；
- 路由 :func:`app.v2.router.get_media` 负责带 MIME 静态服务（Safari 兼容）；
- 只接受有限图片类型 + 体积上限，文件名用 uuid，不信任用户文件名。
"""

from __future__ import annotations

import os
import uuid

_IMAGE_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class MediaStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.upload_dir = os.path.join(data_dir, "uploads")
        os.makedirs(self.upload_dir, exist_ok=True)

    @staticmethod
    def detect_image_ext(data: bytes, content_type: str, filename: str = "") -> str | None:
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype in _IMAGE_MIME_EXT:
            return _IMAGE_MIME_EXT[ctype]
        suffix = os.path.splitext(filename or "")[1].lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return ".jpg" if suffix == ".jpeg" else suffix
        # 魔术字节兜底（有些浏览器 multipart content-type 不可靠）
        head = data[:12]
        if head.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if head.startswith(b"GIF8"):
            return ".gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        return None

    def save_image(self, data: bytes, content_type: str, filename: str = "") -> str:
        if not data:
            raise ValueError("图片为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片太大（上限 8MB）")
        ext = self.detect_image_ext(data, content_type, filename)
        if ext is None:
            raise ValueError("只支持 JPG / PNG / WebP / GIF 图片")
        name = f"img_{uuid.uuid4().hex}{ext}"
        with open(self.path_for(name), "wb") as fh:
            fh.write(data)
        return name

    def path_for(self, filename: str) -> str:
        name = os.path.basename(filename or "")
        if not name or name in (".", ".."):
            raise ValueError("非法媒体文件名")
        return os.path.join(self.upload_dir, name)

    @staticmethod
    def url_for(filename: str | None) -> str | None:
        if not filename:
            return None
        return f"/api/v2/media/{os.path.basename(filename)}"

    @staticmethod
    def mime_for(filename: str) -> str:
        ext = os.path.splitext(os.path.basename(filename or ""))[1].lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(ext, "application/octet-stream")
