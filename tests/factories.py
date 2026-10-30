"""In-memory test images built with Pillow, including the nasty ones."""

import io
import struct
import zlib

from PIL import Image, ImageCms


def _save(im, fmt, **options):
    buf = io.BytesIO()
    im.save(buf, fmt, **options)
    return buf.getvalue()


def srgb_icc():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def jpeg(size=(64, 48), color="tomato", **options):
    return _save(Image.new("RGB", size, color), "JPEG", **options)


def png(size=(32, 32), color="mintcream"):
    return _save(Image.new("RGB", size, color), "PNG")


def webp(size=(32, 32), color="purple"):
    return _save(Image.new("RGB", size, color), "WEBP")


def jpeg_phone(size=(40, 20)):
    """A JPEG that must be rotated (Orientation=6), carries GPS + maker EXIF and an ICC profile."""
    im = Image.new("RGB", size, "red")
    exif = Image.Exif()
    exif[0x0112] = 6                       # Orientation: rotate 90 CW to view
    exif[0x010F] = "PhoneMaker"            # Make
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (40.0, 26.0, 46.0)
    return _save(im, "JPEG", exif=exif.tobytes(), icc_profile=srgb_icc())


def mpo(size=(40, 30)):
    first = Image.new("RGB", size, "orange")
    second = Image.new("RGB", size, "blue")
    return _save(first, "MPO", save_all=True, append_images=[second])


def cmyk_jpeg(size=(24, 24)):
    return _save(Image.new("CMYK", size, (0, 255, 255, 0)), "JPEG")


def rgba_png(size=(24, 24)):
    return _save(Image.new("RGBA", size, (255, 0, 0, 100)), "PNG")


def palette_png_with_transparency(size=(24, 24)):
    im = Image.new("P", size, 0)
    im.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    return _save(im, "PNG", transparency=0)


def png_16bit(size=(16, 16)):
    return _save(Image.new("I;16", size, 40000), "PNG")


def animated_webp(size=(20, 20)):
    frames = [Image.new("RGB", size, c) for c in ("red", "green", "blue")]
    return _save(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0)


def gif(size=(10, 10)):
    return _save(Image.new("RGB", size, "yellow"), "GIF")


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_header_only(width, height):
    """A tiny file whose header claims an enormous image (a decompression bomb)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 64)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def bomb_png():
    return png_header_only(20000, 20000)       # 400M pixels: Pillow's own hard limit


def big_pixels_png():
    return png_header_only(9000, 8000)         # 72M pixels: over our 60M cap, under Pillow's error


def jpeg_with_payload():
    return jpeg() + b"<script>alert('polyglot')</script>"


def text_as_jpg():
    return b"this is definitely not a photo\n" * 10


def fake_heic():
    """The start of an HEIC file (an ISO-BMFF 'ftyp' box with brand 'heic'), which Pillow can't open."""
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64


def png_with_color_key(mode):
    """A PNG whose transparency is a tRNS color key rather than an alpha channel."""
    if mode == "RGB":
        im = Image.new("RGB", (8, 8), (255, 0, 0))
        return _save(im, "PNG", transparency=(255, 0, 0))
    im = Image.new(mode, (8, 8), 0)
    return _save(im, "PNG", transparency=0)
