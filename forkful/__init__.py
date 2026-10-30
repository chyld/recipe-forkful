"""Forkful: cook it, fork it, love it."""

import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, render_template, request, url_for
from werkzeug.exceptions import HTTPException

__version__ = "1.12.0"   # bump on every release; shown in the footer

# The Docker build writes the build time here (see Dockerfile); running from a checkout, it's absent.
BUILD_FILE = Path(__file__).with_name("BUILD")

MB = 1024 * 1024

CSP = (
    "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# Friendly copy for every error page: (emoji, headline, body).
ERROR_PAGES = {
    400: ("🥴", "That didn't look right", "Something in that request was off. Go back, refresh the page, and try again."),
    404: ("🔍", "Nothing cooking here", "We couldn't find that page. It may never have existed."),
    405: ("🙅", "Not like that", "That page can't be used that way."),
    413: ("🐘", "Whoa, that's a big one", "That upload was too large, so nothing was saved. "
          "Try fewer or smaller photos per save; you can add more in the next one."),
    429: ("🍪", "Whoa, too many tries!", "Take a snack break and try again in a few minutes."),
    500: ("🔥", "Something burned", "Something went wrong on our side. Please try again."),
    503: ("🧑‍🍳", "Someone's in the kitchen", "Forkful is busy saving something else. Please try again in a moment."),
}


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATA_DIR=os.environ.get("FORKFUL_DATA_DIR", "data"),
        # Upload limits. Waitress is configured 1 MB higher so Flask's friendly 413 page wins.
        MAX_CONTENT_LENGTH=200 * MB,
        MAX_FORM_MEMORY_SIZE=2 * MB,
        MAX_FORM_PARTS=2000,
        MAX_SMALL_FORM_BYTES=256 * 1024,    # non-multipart POSTs: login, logout, fork
        MAX_PHOTO_BYTES=30 * MB,
        MAX_PHOTO_PIXELS=60_000_000,
        PHOTO_MAX_EDGE=4096,
        MAX_EXPORT_BYTES=40 * MB,           # a downloaded recipe file, photos included (PLAN.md §29)
        # Sessions: plain HTTP, so Secure is deliberately off (see PLAN.md §11).
        SESSION_COOKIE_NAME="forkful",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SEND_FILE_MAX_AGE_DEFAULT=3600,
        # Login throttle.
        THROTTLE_WINDOW=timedelta(minutes=15),
        THROTTLE_PAIR_LIMIT=10,
        THROTTLE_IP_LIMIT=50,
        CLOCK=lambda: datetime.now(timezone.utc),
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault("BUILD", BUILD_FILE.read_text().strip() if BUILD_FILE.is_file() else "")
    app.logger.setLevel(logging.INFO)   # the root logger defaults to WARNING, which would hide our INFO lines

    data_dir = Path(app.config["DATA_DIR"])
    app.config["DATABASE"] = data_dir / "forkful.db"
    app.config["PHOTOS_DIR"] = data_dir / "photos"
    app.config["PHOTOS_DIR"].mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = load_secret_key(data_dir)

    from . import auth, cli, db, ids, photos, recipes

    # Before the blueprints add rules that use them: <rid:public_id> and <key:file_key>.
    app.url_map.converters["rid"] = ids.RecipeIdConverter
    app.url_map.converters["key"] = ids.FileKeyConverter
    db.init_app(app)
    auth.init_app(app)
    cli.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(recipes.bp)
    app.register_blueprint(photos.bp)

    app.after_request(add_security_headers)
    register_error_pages(app)
    register_template_helpers(app)
    return app


FLAVORS = ["tomato", "tangerine", "lemon", "mint", "blueberry", "grape", "bubblegum"]


def register_template_helpers(app):
    from .db import parse_ts

    @app.context_processor
    def version_info():
        return {"app_version": __version__, "app_build": app.config["BUILD"]}

    @app.template_global()
    def static_url(filename):
        """A static URL that changes whenever the file can have changed, so browsers never mix a
        new page with an old cached stylesheet or script: the build stamp in Docker, else mtime."""
        token = app.config["BUILD"]
        if not token:
            try:
                token = str(int((Path(app.static_folder) / filename).stat().st_mtime))
            except OSError:
                token = __version__
        return url_for("static", filename=filename, v=token)

    @app.template_filter("flavor")
    def flavor_class(recipe_id):
        """Each recipe's accent color: a closed set of classes, so no inline styles are needed."""
        return f"flavor-{recipe_id % len(FLAVORS)}"

    @app.template_filter("tilt")
    def tilt_class(recipe_id):
        return f"tilt-{recipe_id % 5}"

    @app.template_filter("calories")
    def format_calories(value):
        return f"{int(value or 0):,}"

    @app.template_filter("nice_date")
    def nice_date(ts):
        dt = parse_ts(ts)
        return f"{dt:%b} {dt.day}, {dt.year}"


def load_secret_key(data_dir):
    """SECRET_KEY from the environment, else /data/secret_key (created once, atomically, 0600)."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key is not None:
        if len(env_key) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters long.")
        return env_key

    path = data_dir / "secret_key"
    if not path.exists():
        # Write a complete file under a temporary name, then link it into place.
        # os.link fails if the target exists, so concurrent first starts agree on one key.
        tmp = data_dir / f".secret_key.{secrets.token_hex(8)}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(64))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)

    key = path.read_text().strip()
    if len(key) < 32:
        raise RuntimeError(f"{path} is too short to be a secret key; delete it to generate a new one.")
    return key


def add_security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    return response


def register_error_pages(app):
    def render_error(code, headers=None):
        # Uncommon codes borrow the generic client or server page but keep their own status.
        page = ERROR_PAGES.get(code) or ERROR_PAGES[400 if code < 500 else 500]
        emoji, title, message = page
        html = render_template("error.html", code=code, emoji=emoji, title=title, message=message)
        return html, code, headers or {}

    @app.errorhandler(HTTPException)
    def handle_http_error(exc):
        headers = {}
        if exc.code == 405 and getattr(exc, "valid_methods", None):
            headers["Allow"] = ", ".join(exc.valid_methods)
        return render_error(exc.code or 500, headers)

    @app.errorhandler(sqlite3.OperationalError)
    def handle_db_busy(exc):
        if "locked" in str(exc) or "busy" in str(exc):
            app.logger.warning("Database busy on %s %s", request.method, request.path)
            return render_error(503)
        app.logger.exception("Database error on %s %s", request.method, request.path)
        return render_error(500)

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        # Log the traceback server-side only; never show it, and never log request bodies.
        app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        return render_error(500)
