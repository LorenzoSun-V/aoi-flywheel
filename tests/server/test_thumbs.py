"""Thumbnails of image objects (GET /api/v1/objects/<sha256>/thumb): made by the server, kept in a cache
beside the store's objects, refused with a reason for what is not an image or too large."""
import io
import struct
import zlib

import httpx
from PIL import Image

from fw.api import Api


def picture(fmt: str, w: int, h: int, mode: str = 'RGB', color=(40, 120, 200)) -> bytes:
    out = io.BytesIO()
    Image.new(mode, (w, h), color).save(out, fmt)
    return out.getvalue()


def png_claiming(w: int, h: int) -> bytes:
    """A PNG whose header says w x h: what an image that large looks like before it is decoded."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' * 16)) + chunk(b'IEND', b''))


def thumb(service, token, digest, **params):
    return httpx.get(f'{service.url}/api/v1/objects/{digest}/thumb', params=params,
                     headers={'Authorization': f'Bearer {token}'} if token else {})


def opened(r) -> Image.Image:
    assert r.status_code == 200, r.text
    assert r.headers['content-type'] == 'image/jpeg' and 'immutable' in r.headers['cache-control']
    return Image.open(io.BytesIO(r.content))


def test_a_thumbnail_is_a_small_jpeg_of_the_image_kept_for_the_next_time(service, api, tokens):
    bmp = api.put_bytes(picture('BMP', 1200, 800))
    r = thumb(service, tokens['field'], bmp)
    image = opened(r)
    assert image.format == 'JPEG' and image.size == (256, 171) and len(r.content) < 20_000
    kept = service.settings.store / 'thumbs' / '256' / bmp[:2] / f'{bmp}.jpg'
    assert kept.read_bytes() == r.content                        # kept beside the store, not in objects/
    assert not list((service.settings.store / 'objects').rglob(f'*{bmp}.jpg'))
    again = thumb(service, tokens['algo'], bmp)
    assert again.content == r.content and again.headers['content-type'] == 'image/jpeg'
    kept.unlink()                                                # a cache: a lost thumbnail is made again
    assert thumb(service, tokens['algo'], bmp).content == r.content and kept.is_file()
    # other sizes and formats, never enlarged
    jpg = api.put_bytes(picture('JPEG', 2000, 1000))
    assert opened(thumb(service, tokens['field'], jpg, size=128)).size == (128, 64)
    assert opened(thumb(service, tokens['field'], jpg, size=512)).size == (512, 256)
    small = api.put_bytes(picture('PNG', 100, 50))
    assert opened(thumb(service, tokens['field'], small)).size == (100, 50)
    tif = api.put_bytes(picture('TIFF', 300, 300))               # the browsers show no TIFF; its thumbnail shows
    assert opened(thumb(service, tokens['field'], tif)).size == (256, 256)


def test_a_thumbnail_looks_like_the_image_in_a_browser(service, api, tokens):
    grey16 = api.put_bytes(picture('PNG', 600, 400, 'I;16', 0x8000))   # an industrial camera's 16-bit grey
    image = opened(thumb(service, tokens['field'], grey16))
    assert image.mode == 'L' and abs(image.getpixel((10, 10)) - 128) <= 2
    clear = api.put_bytes(picture('PNG', 300, 200, 'RGBA', (0, 0, 0, 0)))  # transparency shows on white
    assert opened(thumb(service, tokens['field'], clear)).convert('RGB').getpixel((5, 5)) >= (250, 250, 250)
    out = io.BytesIO()
    Image.new('RGB', (400, 200), (200, 0, 0)).save(out, 'JPEG', exif=b'Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x01'
                                                     b'\x01\x12\x00\x03\x00\x00\x00\x01\x00\x06\x00\x00\x00\x00\x00\x00')
    turned = api.put_bytes(out.getvalue())                       # EXIF orientation 6: shown turned upright
    assert opened(thumb(service, tokens['field'], turned)).size == (128, 256)


def test_a_thumbnail_needs_an_account_that_may_download(service, api, tokens):
    bmp = api.put_bytes(picture('BMP', 64, 64))
    for token in (None, 'not-a-token'):
        r = thumb(service, token, bmp)
        assert r.status_code == 401 and 'thumbs' not in r.text


def test_what_is_no_image_or_too_large_is_refused_with_its_reason(service, api, tokens):
    text = api.put_bytes(b'not an image at all\n')
    r = thumb(service, tokens['field'], text)
    assert r.status_code == 415 and r.json()['detail'] == f'无法为文件 {text[:12]} 生成缩略图：不是可识别的图片，或图片已损坏。'
    big = api.put_bytes(png_claiming(20000, 10000))
    r = thumb(service, tokens['field'], big)
    assert r.status_code == 422 and r.json()['detail'] == f'无法为文件 {big[:12]} 生成缩略图：图片为 20000×10000 像素，超过 150 百万像素。'
    huge = api.put_bytes(png_claiming(30000, 30000))             # past what Pillow opens at all
    r = thumb(service, tokens['field'], huge)
    assert r.status_code == 422 and '超过 150 百万像素' in r.json()['detail']
    r = thumb(service, tokens['field'], text, size=300)
    assert r.status_code == 400 and r.json()['detail'] == '缩略图的尺寸只能是 128、256、512。'
    r = thumb(service, tokens['field'], 'ab' * 32)
    assert r.status_code == 404 and r.json()['detail'] == f'服务器上没有文件 {"ab" * 6}。'
    assert thumb(service, tokens['field'], 'not-a-digest').status_code == 400
    assert not (service.settings.store / 'thumbs' / '256' / text[:2] / f'{text}.jpg').exists()


def test_while_the_store_refuses_writes_thumbnails_are_made_but_not_kept(make_service, tokens):
    svc = make_service()
    bmp = Api(svc.url, tokens['algo']).put_bytes(picture('BMP', 640, 480))
    (svc.settings.store / 'WRITE-OK').unlink()
    image = opened(thumb(svc, tokens['field'], bmp))
    assert image.size == (256, 192)
    assert not (svc.settings.store / 'thumbs').exists()


def test_a_thumbnail_that_cannot_be_kept_is_still_given(make_service, tokens):
    svc = make_service()
    bmp = Api(svc.url, tokens['algo']).put_bytes(picture('BMP', 640, 480))
    (svc.settings.store / 'thumbs').write_text('a file where the folder goes')  # every write of the cache fails
    assert opened(thumb(svc, tokens['field'], bmp)).size == (256, 192)
