"""A recipe as one self-contained HTML file: styles, font and photos all inside, nothing to fetch.

The file carries its own Content-Security-Policy (only data: images and fonts, only the one
style block, no script), so opening it never touches the network. See PLAN.md §29.
"""

import base64
import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote

from flask import current_app, render_template
from markupsafe import Markup

from . import MB, photos
from .db import utc_ts

STYLESHEETS = ("css/forkful.css", "css/export.css")
FONT_FILE = "fonts/fredoka-latin-wght.woff2"
FONT_URL = f'url("../{FONT_FILE}")'              # exactly as forkful.css writes it
URL_RE = re.compile(r"""url\(\s*['"]?([^'")]*)""", re.I)

PHOTO_MAX_EDGE = 1600
PHOTO_QUALITY = 82

SLUG_MAX = 60
WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}

POLICY = ("default-src 'none'; img-src data:; font-src data:; style-src '{style}'; script-src 'none'; "
          "object-src 'none'; base-uri 'none'; form-action 'none'")


class ExportError(Exception):
    """A user-facing reason the download couldn't be made. Nothing partial is ever sent."""


def stylesheet():
    """forkful.css + export.css with the font inlined. Fails closed if anything else would load."""
    static = Path(current_app.static_folder)
    css = "\n".join((static / name).read_text(encoding="utf-8") for name in STYLESHEETS)
    if css.count(FONT_URL) != 1:
        raise RuntimeError("export: the font url() in forkful.css changed; update forkful/export.py")
    font = base64.b64encode((static / FONT_FILE).read_bytes()).decode("ascii")
    css = css.replace(FONT_URL, f'url("data:font/woff2;base64,{font}")')
    outside = [u for u in URL_RE.findall(css) if not u.strip().startswith("data:")]
    # A CSS escape could spell url( or @import in a way the checks above can't see; ours use none.
    if outside or "@import" in css.lower() or "</style" in css.lower() or "\\" in css:
        raise RuntimeError(f"export: the stylesheet would load something from outside the file: {outside}")
    return css


def style_hash(css):
    return "sha256-" + base64.b64encode(hashlib.sha256(css.encode("utf-8")).digest()).decode("ascii")


def policy(css):
    """The file's CSP. The template prints `css` byte for byte, so the hash matches."""
    return POLICY.format(style=style_hash(css))


def too_big(budget):
    return ExportError(f"This recipe would make a file bigger than {budget // MB} MB, too much for one "
                       "download. Delete a few photos and try again.")


def embed_photos(rows, used):
    """Every photo as a data: URI, in order, or ExportError. `used` is what the rest of the file
    takes; each photo's size is checked before its base64 copy is made."""
    budget = current_app.config["MAX_EXPORT_BYTES"]
    embedded = []
    for row in rows:
        try:
            content_type, data, width, height = photos.export_copy(
                photos.photo_path(row["file_key"], row["content_type"]),
                current_app.config["MAX_PHOTO_PIXELS"], PHOTO_MAX_EDGE, PHOTO_QUALITY)
        except photos.PhotoError:
            current_app.logger.warning("Download: photo %s could not be read", row["file_key"])
            raise ExportError("One of this recipe's photos couldn't be read, so nothing was downloaded. "
                              "Try again, or delete that photo in Edit recipe.") from None
        prefix = f"data:{content_type};base64,"
        used += len(prefix) + 4 * -(-len(data) // 3)        # the base64 length, before making it
        if used > budget:
            raise too_big(budget)
        embedded.append({"src": prefix + base64.b64encode(data).decode("ascii"), "width": width, "height": height})
    return embedded


def render(recipe, ingredients, tags, total, photos, parent):
    """The whole file as UTF-8 bytes, and its CSP (for the response header too). The size limit
    is for the finished file: styles, font and text count, not just the photos."""
    css = stylesheet()
    csp = policy(css)
    embedded = embed_photos(photos, used=len(css))
    page = render_template(
        "export.html",
        css=Markup(css),            # our own static files, checked above; a <style> body is never escaped
        csp=csp,
        recipe=recipe,
        ingredients=ingredients,
        tags=tags,
        total=total,
        cover=embedded[0] if embedded else None,
        more_photos=embedded[1:],
        parent=parent,
        saved_on=utc_ts(),
    ).encode("utf-8")
    if len(page) > current_app.config["MAX_EXPORT_BYTES"]:
        raise too_big(current_app.config["MAX_EXPORT_BYTES"])
    return page, csp


def slug(text):
    """Lowercase letters, digits and their combining marks (Hindi vowel signs, accents), with runs
    of anything else as one "-". At most SLUG_MAX characters; never empty; never a Windows device."""
    text = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).lower())
    kept = "".join(c if c.isalnum() or unicodedata.category(c).startswith("M") else "-" for c in text)
    words = re.sub(r"-+", "-", kept).strip("-")[:SLUG_MAX].rstrip("-")
    if not words:
        return "recipe"
    # Windows reserves con, com1 ... and also com¹, lpt² ..., with any extension, in every folder.
    return f"recipe-{words}" if unicodedata.normalize("NFKC", words) in WINDOWS_DEVICES else words


def filenames(name):
    """(ASCII fallback, Unicode) download names: ("creme-brulee.html", "crème-brûlée.html")."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return f"{slug(plain)}.html", f"{slug(name)}.html"


def disposition(name):
    """Content-Disposition parameters for headers.set(): the RFC 6266 attachment form."""
    fallback, full = filenames(name)
    options = {"filename": fallback}
    if full != fallback:
        options["filename*"] = "UTF-8''" + quote(full, safe="")
    return options
