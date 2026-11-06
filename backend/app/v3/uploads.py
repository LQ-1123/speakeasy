"""用户图片上传：校验 → 去除 EXIF → 缩放 → webp 落盘。

只接受真实图片（Pillow 解码），限制 8MB / 1600px；文件名按内容哈希，天然去重。
存储路径 ``data/uploads/{user_hash}/{digest}.webp``，对外 ``/uploads/...``。
"""

from __future__ import annotations

import hashlib
import io
import os

from PIL import Image, ImageOps

from app.logging_utils import get_logger

log = get_logger(__name__)

MAX_BYTES = 8 * 1024 * 1024
MAX_SIDE = 1600
QUALITY = 85
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP"}


class UploadError(ValueError):
    """上传失败：内容不是图片 / 太大 / 无法解码。"""


def user_hash(user_id: str) -> str:
    return hashlib.sha1(("user:" + (user_id or "local")).encode("utf-8")).hexdigest()[:12]


def save_user_image(raw: bytes, user_id: str, uploads_root: str) -> dict:
    """保存一张用户图片，返回 {url,width,height,bytes}。"""
    if not raw:
        raise UploadError("图片内容为空")
    if len(raw) > MAX_BYTES:
        raise UploadError("图片太大（上限 8MB）")

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise UploadError("无法识别这张图片，请换一张 JPG/PNG/WebP") from exc

    if image.format not in ALLOWED_FORMATS:
        raise UploadError("只支持 JPG / PNG / WebP / GIF 图片")

    # 手机照片按 EXIF 方向摆正，并在重存时丢掉 EXIF/GPS 等隐私信息
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE))

    digest = hashlib.sha256(raw).hexdigest()[:16]
    folder = os.path.join(uploads_root, user_hash(user_id))
    os.makedirs(folder, exist_ok=True)
    filename = f"{digest}.webp"
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        image.save(path, "WEBP", quality=QUALITY, method=4)
    log.info("用户图片已保存 %s (%dx%d)", path, image.width, image.height)
    return {
        "url": f"/uploads/{user_hash(user_id)}/{filename}",
        "width": image.width,
        "height": image.height,
        "bytes": os.path.getsize(path),
    }


__all__ = ["MAX_BYTES", "UploadError", "save_user_image", "user_hash"]
