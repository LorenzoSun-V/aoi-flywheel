"""Small previews of the images in the store, for the web pages: a field BMP is a few MB and a page of a
version shows 60 of them.

A thumbnail is made with Pillow from the object in the store: the image as the browser shows it (EXIF
orientation applied, 16-bit grey taken to its high 8 bits, transparency on white), its longer side at most one
of SIZES (never enlarged), as a JPEG. It is kept in <store>/thumbs/<size>/<ab>/<sha256>.jpg. That folder is a
cache, not part of the store: nothing in the database refers to it, backup.sh copies only objects/, and it can
be deleted at any time; a missing thumbnail is made again on the next request. While the store refuses writes
(store.problem()), thumbnails are made but not kept; a failed write is logged and changes nothing else.

Only the formats of IMAGE_EXTS are decoded, whatever else an object holds. Images over MAX_PIXELS are refused
rather than decoded whole. A large frame takes a few hundred MB while it is decoded, so at most AT_ONCE
thumbnails are made at the same time; a request that waits longer than WAIT seconds for its turn is refused
(503). Each image is shrunk in the mode it was decoded in and only then turned into 8 bits: a 16-bit frame is
never converted at full size.
"""
from __future__ import annotations

import io
import logging
import os
import struct
import threading
import warnings
from pathlib import Path
from typing import Any, Optional, Tuple

from PIL import Image, UnidentifiedImageError

log = logging.getLogger('fwserver.thumbs')

SIZES = (128, 256, 512)
MAX_PIXELS = 150_000_000
QUALITY = 85
FORMATS = ('JPEG', 'PNG', 'BMP', 'TIFF', 'WEBP')  # the images of IMAGE_EXTS (fw.contract); no other decoder runs
AT_ONCE = 2
WAIT = 60.0
ORIENTATION = 0x0112  # the EXIF tag, and the turn each of its values asks for (as ImageOps.exif_transpose)
TURNS = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
         5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
         8: Image.Transpose.ROTATE_90}
_making = threading.BoundedSemaphore(AT_ONCE)
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
    if not _making.acquire(timeout=WAIT):
        raise ThumbError(503, '服务器正在生成其他缩略图，请稍后刷新页面。')
    try:
        if cached.is_file():  # made by another request while this one waited
            return cached, None
        data = make(source, size, digest[:12])
    finally:
        _making.release()
    if store.problem() is None:
        try:
            _keep(cached, data)
        except OSError as e:
            log.warning('thumbnail %s not kept: %s', cached, e)
    return None, data


def make(path: Path, size: int, name: str) -> bytes:
    """The JPEG thumbnail of the image file at path; name: how the file is called in errors."""
    try:
        with Image.open(path, formats=FORMATS) as image:
            w, h = image.size
            if w * h > MAX_PIXELS:
                raise _too_big(name, w, h)
            turn = TURNS.get(image.getexif().get(ORIENTATION))  # only this tag: the EXIF is not written again
            image.draft('RGB', (size, size))  # a JPEG is decoded at a reduced scale
            small = _resizable(image)
            # reduce() first, then a smooth resize; reduce() takes no 16-bit grey, which is resized directly
            small.thumbnail((size, size), Image.Resampling.BICUBIC,
                            reducing_gap=None if small.mode.startswith('I;16') else 2.0)
            shown = _eight_bit(small.transpose(turn) if turn is not None else small)
            out = io.BytesIO()
            shown.save(out, 'JPEG', quality=QUALITY, optimize=True)
            return out.getvalue()
    except Image.DecompressionBombError:
        raise ThumbError(422, f'无法为文件 {name} 生成缩略图：图片超过 {MAX_PIXELS // 1_000_000} 百万像素。') from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, struct.error, KeyError, IndexError,
            TypeError, ZeroDivisionError) as e:  # what Pillow raises on broken headers and EXIF
        raise ThumbError(415, f'无法为文件 {name} 生成缩略图：不是可识别的图片，或图片已损坏。') from e


def _too_big(name: str, w: int, h: int) -> ThumbError:
    return ThumbError(422, f'无法为文件 {name} 生成缩略图：图片为 {w}×{h} 像素，超过 {MAX_PIXELS // 1_000_000} 百万像素。')


def _resizable(image: Image.Image) -> Image.Image:
    """The image in a mode Pillow resizes smoothly: a palette or 1-bit image would only take its nearest pixels."""
    if image.mode in ('P', 'PA'):
        return image.convert('RGBA' if image.mode == 'PA' or 'transparency' in image.info else 'RGB')
    if image.mode == '1':
        return image.convert('L')
    return image


def _eight_bit(image: Image.Image) -> Image.Image:
    """The image in RGB or L, as a browser draws it."""
    if image.mode.startswith('I;16') or image.mode == 'I':
        grey = image if image.mode == 'I' else image.convert('I')
        return grey.point(lambda v: v * (1 / 256)).convert('L')  # 16-bit grey: its high 8 bits
    if image.mode in ('RGBA', 'LA'):
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
