"""Small previews of the images in the store, for the web pages: a field BMP is a few MB and a page of a
version shows 60 of them.

A thumbnail is made with Pillow from the object in the store: the image as the browser shows it (EXIF
orientation applied, 16-bit grey taken to its high 8 bits, transparency on white), its longer side at most one
of SIZES (never enlarged), as a JPEG. It is kept in <store>/thumbs/<size>/<ab>/<sha256>.jpg. That folder is a
cache, not part of the store: nothing in the database refers to it, backup.sh copies only objects/, and it can
be deleted at any time; a missing thumbnail is made again on the next request. While the store refuses writes
(store.problem()), thumbnails are made but not kept; a failed write is logged and changes nothing else.
Images over MAX_PIXELS are refused rather than decoded whole.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import warnings
from pathlib import Path
from typing import Any, Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger('fwserver.thumbs')

SIZES = (128, 256, 512)
MAX_PIXELS = 150_000_000
QUALITY = 85
Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow refuses images over twice this outright (DecompressionBombError)
warnings.filterwarnings('ignore', category=Image.DecompressionBombWarning)  # make() refuses those itself


class ThumbError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def cache_path(root: Path, digest: str, size: int) -> Path:
    return Path(root) / 'thumbs' / str(size) / digest[:2] / f'{digest}.jpg'


def thumbnail(store: Any, digest: str, size: int) -> Tuple[Optional[Path], Optional[bytes]]:
    """The thumbnail of the object digest (checked by the caller) at size: (the cached file, None), or (None, the
    JPEG) when it was made now. store: the Store (root, path(digest), problem())."""
    if size not in SIZES:
        raise ThumbError(400, f'缩略图的尺寸只能是 {"、".join(str(s) for s in SIZES)}。')
    cached = cache_path(store.root, digest, size)
    if cached.is_file():
        return cached, None
    source = store.path(digest)
    if not source.is_file():
        raise ThumbError(404, f'服务器上没有文件 {digest[:12]}。')
    data = make(source, size, digest[:12])
    if store.problem() is None:
        try:
            _keep(cached, data)
        except OSError as e:
            log.warning('thumbnail %s not kept: %s', cached, e)
    return None, data


def make(path: Path, size: int, name: str) -> bytes:
    """The JPEG thumbnail of the image file at path; name: how the file is called in errors."""
    try:
        with Image.open(path) as image:
            w, h = image.size
            if w * h > MAX_PIXELS:
                raise _too_big(name, w, h)
            image.draft('RGB', (size, size))  # a JPEG is decoded at a reduced scale
            shown = _eight_bit(ImageOps.exif_transpose(image))
            shown.thumbnail((size, size), Image.Resampling.BICUBIC, reducing_gap=2.0)
            out = io.BytesIO()
            shown.save(out, 'JPEG', quality=QUALITY, optimize=True)
            return out.getvalue()
    except Image.DecompressionBombError:
        raise ThumbError(422, f'无法为文件 {name} 生成缩略图：图片超过 {MAX_PIXELS // 1_000_000} 百万像素。') from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:  # SyntaxError: some broken headers
        raise ThumbError(415, f'无法为文件 {name} 生成缩略图：不是可识别的图片，或图片已损坏。') from e


def _too_big(name: str, w: int, h: int) -> ThumbError:
    return ThumbError(422, f'无法为文件 {name} 生成缩略图：图片为 {w}×{h} 像素，超过 {MAX_PIXELS // 1_000_000} 百万像素。')


def _eight_bit(image: Image.Image) -> Image.Image:
    """The image in RGB or L, as a browser draws it."""
    if image.mode.startswith('I;16') or image.mode == 'I':
        grey = image if image.mode == 'I' else image.convert('I')
        return grey.point(lambda v: v * (1 / 256)).convert('L')  # 16-bit grey: its high 8 bits
    if image.mode in ('RGBA', 'LA', 'PA') or (image.mode == 'P' and 'transparency' in image.info):
        rgba = image.convert('RGBA')
        flat = Image.new('RGB', rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel('A'))
        return flat
    if image.mode in ('RGB', 'L'):
        return image
    return image.convert('RGB')


def _keep(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)  # whole or not at all; two makers of one thumbnail write the same bytes
    finally:
        tmp.unlink(missing_ok=True)
