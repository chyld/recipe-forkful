"""Photos: validate and re-encode uploads with Pillow, store them on disk, serve them only to the
owner, and shrink copies of them for a downloaded recipe (forkful/export.py)."""

import io
import os
import re
import threading
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from flask import Blueprint, abort, current_app, g, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from . import repo
from .db import get_db

bp = Blueprint("photos", __name__)

# Pillow format -> (stored content type, file extension). The one map used to write and to serve.
FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "MPO": ("image/jpeg", "jpg"),    # iPhone HDR/gain-map JPEGs; we keep the first frame
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
ACCEPT = "image/jpeg,image/png,image/webp"
FILE_KEY_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
MAX_ICC_BYTES = 1024 * 1024
QUALITY = 88

# Decoding is the memory-hungry part, so only one photo is decoded at a time per process.
_decode_lock = threading.Lock()


UNSUPPORTED = "Please use a JPEG, PNG, or WebP photo."
TOO_MANY_PIXELS = "That photo has too many pixels. Please use a smaller one."


class PhotoError(Exception):
    """A user-facing problem with one uploaded file."""


@dataclass
class StoredPhoto:
    file_key: str
    content_type: str
    width: int
    height: int
    byte_size: int
    path: Path


def photos_dir():
    return Path(current_app.config["PHOTOS_DIR"])


def photo_path(file_key, content_type):
    if not FILE_KEY_RE.fullmatch(file_key) or content_type not in EXTENSIONS:
        raise ValueError("bad photo key")
    base = photos_dir().resolve()
    path = (base / f"{file_key}.{EXTENSIONS[content_type]}").resolve()
    if path.parent != base:
        raise ValueError("photo path escapes the photo directory")
    return path


def _normalize_mode(im):
    """Convert to a mode the output format can store, keeping transparency where it exists."""
    if im.mode == "CMYK":
        return im.convert("RGB"), False          # a CMYK color profile no longer applies
    if im.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
        return im.convert("I").point(lambda v: v * (1 / 256)).convert("L"), True
    if im.mode == "F":
        return im.convert("L"), True
    if im.mode == "P":
        return im.convert("RGBA" if "transparency" in im.info else "RGB"), True
    if im.mode in ("PA", "LA", "La", "RGBa"):
        return im.convert("RGBA"), True
    if im.mode in ("1", "L", "RGB") and "transparency" in im.info:
        return im.convert("RGBA"), True          # a PNG tRNS color key becomes real alpha
    if im.mode == "1":
        return im.convert("L"), True
    if im.mode not in ("RGB", "RGBA", "L"):
        return im.convert("RGB"), True
    return im, True


@contextmanager
def _decoding():
    """Hold the decode lock, treat decompression-bomb warnings as errors, and turn every
    decoding failure into a PhotoError."""
    with _decode_lock, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            yield
        except PhotoError:
            raise
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise PhotoError(TOO_MANY_PIXELS) from None
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
            # Unreadable, truncated, or a format we don't take (HEIC, GIF, AVIF, ...).
            raise PhotoError(UNSUPPORTED) from None


def _decode(stream, max_pixels, max_edge):
    """Decode, sanity-check, upright and shrink one image, inside _decoding().

    Returns (image, Pillow format, encode options): the options carry the ICC profile
    when it still applies. Every other piece of metadata is dropped.
    """
    with Image.open(stream) as original:
        fmt = original.format                       # capture before exif_transpose drops it
        if fmt not in FORMATS:
            raise PhotoError(UNSUPPORTED)
        if getattr(original, "n_frames", 1) > 1:
            original.seek(0)                        # first frame only; never walk the rest
        width, height = original.size
        if width < 1 or height < 1 or width * height > max_pixels:
            raise PhotoError(TOO_MANY_PIXELS)
        if fmt in ("JPEG", "MPO"):
            original.draft(original.mode, (max_edge, max_edge))   # cheap downscale while decoding
        original.load()
        icc = original.info.get("icc_profile")
        if not isinstance(icc, bytes) or len(icc) > MAX_ICC_BYTES:
            icc = None
        im = ImageOps.exif_transpose(original)
    im, keep_icc = _normalize_mode(im)
    im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    im.info = {}                                   # drop EXIF/XMP/text; only the ICC goes back in
    return im, fmt, ({"icc_profile": icc} if (icc and keep_icc) else {})


def prepare(stream, max_pixels, max_edge):
    """Decode, sanity-check, upright, strip metadata and re-encode one uploaded image.

    Returns (data, content_type, ext, width, height). Raises PhotoError for anything
    that isn't a reasonable JPEG/PNG/WebP photo.
    """
    with _decoding():
        im, fmt, options = _decode(stream, max_pixels, max_edge)
        content_type, ext = FORMATS[fmt]
        out = io.BytesIO()
        if content_type == "image/jpeg":
            _save_jpeg(im, out, QUALITY, options)
        elif content_type == "image/png":
            im.save(out, format="PNG", **options)
        else:
            im.save(out, format="WEBP", quality=QUALITY, **options)
        return out.getvalue(), content_type, ext, im.width, im.height


def export_copy(path, max_pixels, max_edge, quality):
    """A stored photo shrunk for embedding in a downloaded recipe (forkful/export.py).

    Returns (content_type, data, width, height): JPEG, or PNG when the photo really uses
    transparency. Never upscales. Raises PhotoError if the file is missing or can't be read.
    """
    with _decoding(), open(path, "rb") as stream:
        im, _fmt, options = _decode(stream, max_pixels, max_edge)
        out = io.BytesIO()
        if im.mode == "RGBA" and im.getchannel("A").getextrema()[0] < 255:
            im.save(out, format="PNG", optimize=True, **options)
            return "image/png", out.getvalue(), im.width, im.height
        _save_jpeg(im, out, quality, options)
        return "image/jpeg", out.getvalue(), im.width, im.height


def _save_jpeg(im, out, quality, options):
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(out, format="JPEG", quality=quality, optimize=True, **options)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_uploads(files):
    """Validate and write every uploaded file, or none of them.

    Returns [StoredPhoto]. On any failure, unlinks what this call wrote and raises
    PhotoError naming the file that failed.
    """
    max_bytes = current_app.config["MAX_PHOTO_BYTES"]
    stored = []
    try:
        for upload in files:
            label = (upload.filename or "a photo")[:80]
            stream = upload.stream
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(0)
            if size == 0:
                raise PhotoError(f"{label}: that file is empty.")
            if size > max_bytes:
                raise PhotoError(f"{label}: photos can be up to {max_bytes // (1024 * 1024)} MB each.")
            try:
                data, content_type, _ext, width, height = prepare(
                    stream, current_app.config["MAX_PHOTO_PIXELS"], current_app.config["PHOTO_MAX_EDGE"])
            except PhotoError as exc:
                raise PhotoError(f"{label}: {exc}") from None
            file_key = uuid.uuid4().hex
            path = photo_path(file_key, content_type)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            stored.append(StoredPhoto(file_key, content_type, width, height, len(data), path))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        if stored:
            _fsync_dir(photos_dir())
        return stored
    except BaseException:
        unlink_files([p.path for p in stored])
        raise


def unlink_files(paths):
    for path in paths:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            current_app.logger.warning("Photo file already missing: %s", Path(path).name)
        except OSError:
            current_app.logger.exception("Could not delete photo file %s", Path(path).name)


def unlink_photo_rows(rows):
    """Remove files for deleted (file_key, content_type) rows, after the deleting commit."""
    unlink_files([photo_path(key, content_type) for key, content_type in rows])


@bp.route("/photos/<file_key>")
def serve(file_key):
    if not FILE_KEY_RE.fullmatch(file_key):
        abort(404)
    row = repo.get_photo_by_key(get_db(), g.user["id"], file_key)
    if row is None:
        abort(404)
    path = photo_path(row["file_key"], row["content_type"])
    if not path.is_file():
        abort(404)
    response = send_file(path, mimetype=row["content_type"], conditional=True, etag=True)
    # Never public: every reuse must come back through the ownership check (cheap 304 if unchanged).
    # send_file also sets an Expires an hour out; drop it so no cache treats the photo as fresh.
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers.pop("Expires", None)
    response.vary.add("Cookie")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
