"""Login, logout, server-side sessions, CSRF, login throttling, and deny-by-default auth."""

import hashlib
import hmac
import re
import secrets
import threading
from collections import OrderedDict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)

from .db import get_db, now, transaction, utc_ts

bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}", re.ASCII)
MIN_PASSWORD = 5
MAX_PASSWORD = 1024

# Endpoints reachable without logging in. Everything else requires a session.
PUBLIC_ENDPOINTS = {"auth.login", "static"}

LOGIN_FAILED = "That username and password don't match. Check them and try again."

_hasher = PasswordHasher()
_dummy_hash = None
_dummy_lock = threading.Lock()


# --- passwords -------------------------------------------------------------

def hash_password(password):
    return _hasher.hash(password)


def verify_password(password_hash, password):
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def dummy_hash():
    """A real Argon2id hash with current parameters, so unknown users cost the same as known ones."""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash(secrets.token_hex(16))
        return _dummy_hash


def valid_username(username):
    return bool(USERNAME_RE.fullmatch(username))


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# --- login throttle ----------------------------------------------------------

INVALID_USERNAME_KEY = "<invalid>"


class _FailureLog:
    """Failure times per key within a sliding window, holding at most max_keys keys."""

    def __init__(self, window, max_keys):
        self.window = window
        self.max_keys = max_keys
        self.entries = OrderedDict()  # key -> deque of failure times, least recently failed first

    def count(self, key, now_):
        times = self.entries.get(key)
        if times is None:
            return 0
        cutoff = now_ - self.window
        while times and times[0] <= cutoff:
            times.popleft()
        if not times:
            del self.entries[key]
            return 0
        return len(times)

    def add(self, key, now_):
        self.count(key, now_)
        self.entries.setdefault(key, deque()).append(now_)
        self.entries.move_to_end(key)
        # Keys are ordered by most recent failure, so this evicts the stalest first.
        while len(self.entries) > self.max_keys:
            self.entries.popitem(last=False)


class LoginThrottle:
    """In-memory failure counter, keyed by (ip, username) pair and by ip alone.

    Waitress runs one process, so memory is enough; a restart clears it. Keys are
    bounded in size (usernames that fail validation share one key) and in number,
    and the per-ip and per-pair logs are separate so filling one can't evict the other.
    """

    def __init__(self, window, pair_limit, ip_limit, max_keys=10_000):
        self.pair_limit = pair_limit
        self.ip_limit = ip_limit
        self._lock = threading.Lock()
        self._pairs = _FailureLog(window, max_keys)
        self._ips = _FailureLog(window, max_keys)

    @staticmethod
    def _pair_key(ip, username):
        name = username.lower() if valid_username(username) else INVALID_USERNAME_KEY
        return (ip, name)

    def blocked(self, ip, username, now_):
        with self._lock:
            return (self._pairs.count(self._pair_key(ip, username), now_) >= self.pair_limit
                    or self._ips.count(ip, now_) >= self.ip_limit)

    def record_failure(self, ip, username, now_):
        with self._lock:
            self._pairs.add(self._pair_key(ip, username), now_)
            self._ips.add(ip, now_)

    def reset(self, ip, username):
        with self._lock:
            self._pairs.entries.pop(self._pair_key(ip, username), None)


def throttle():
    return current_app.extensions["forkful_throttle"]


# --- CSRF --------------------------------------------------------------------

def csrf_token():
    """The session's CSRF token, created on first use (including for the anonymous login form)."""
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf():
    expected = session.get("csrf")
    sent = request.form.get("csrf_token", "")
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, which would be a 500.
    if (not isinstance(expected, str) or not expected or not isinstance(sent, str)
            or not hmac.compare_digest(expected.encode(), sent.encode())):
        abort(400)


# --- request hooks -----------------------------------------------------------

def load_user():
    g.user = None
    sid = session.get("sid")
    if not isinstance(sid, str) or not sid:
        return
    row = get_db().execute(
        "SELECT users.id, users.username FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
        (token_hash(sid), utc_ts()),
    ).fetchone()
    if row is None:
        session.pop("sid", None)
        return
    g.user = row


def require_login_and_csrf():
    """Deny by default: every endpoint except PUBLIC_ENDPOINTS needs a session.

    Order matters: anonymous visitors are redirected to log in; logged-in POSTs
    (and the login form itself) must carry a valid CSRF token before any route
    code, including ownership checks, runs.
    """
    if request.endpoint is None:
        return None  # 404/405 are handled by the error pages
    if request.endpoint not in PUBLIC_ENDPOINTS and g.user is None:
        return redirect(url_for("auth.login"))
    if request.method == "POST" and (g.user is not None or request.endpoint == "auth.login"):
        check_csrf()
    return None


def limit_small_form_bodies():
    """Werkzeug's per-field and part-count limits only apply to multipart bodies.

    Every non-multipart POST here (login, logout, fork) is tiny, so cap those bodies
    before anything parses them; otherwise one anonymous request could buffer up to
    MAX_CONTENT_LENGTH of urlencoded fields in memory.
    """
    if request.method == "POST" and request.mimetype != "multipart/form-data":
        request.max_content_length = current_app.config["MAX_SMALL_FORM_BYTES"]


def init_app(app):
    app.extensions["forkful_throttle"] = LoginThrottle(
        window=app.config["THROTTLE_WINDOW"],
        pair_limit=app.config["THROTTLE_PAIR_LIMIT"],
        ip_limit=app.config["THROTTLE_IP_LIMIT"],
    )
    app.extensions["forkful_seen_login_ip"] = False
    dummy_hash()  # pay the one-time hashing cost at startup, not on the first unknown-user login
    app.before_request(limit_small_form_bodies)
    app.before_request(load_user)
    app.before_request(require_login_and_csrf)
    app.jinja_env.globals["csrf_token"] = csrf_token


# --- routes ------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("recipes.index"))
    if request.method == "GET":
        return render_template("login.html", username="", error=None)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    ip = request.remote_addr or "unknown"
    when = now()

    if not current_app.extensions["forkful_seen_login_ip"]:
        current_app.extensions["forkful_seen_login_ip"] = True
        current_app.logger.info(
            "First login attempt from %s. If this is always a Docker gateway address, "
            "client IPs are hidden and throttling falls back to per-username.", ip)

    if throttle().blocked(ip, username, when):
        abort(429)

    user = None
    if valid_username(username) and 0 < len(password) <= MAX_PASSWORD:
        row = get_db().execute(
            "SELECT id, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is not None:
            if verify_password(row["password_hash"], password):
                user = row
        else:
            verify_password(dummy_hash(), password)

    if user is not None and start_session(user):
        throttle().reset(ip, username)
        return redirect(url_for("recipes.index"))

    throttle().record_failure(ip, username, when)
    return render_template("login.html", username=username[:32], error=LOGIN_FAILED)


def start_session(user):
    """Create a session, but only if the password hash we verified is still current.

    If `flask set-password` changed it mid-login, the INSERT matches no row and the
    login fails, so a revoked password can never mint a new session.
    """
    token = secrets.token_urlsafe(32)
    created = now()
    expires = created + current_app.config["PERMANENT_SESSION_LIFETIME"]
    db = get_db()
    with transaction(db):
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_ts(created),))
        cur = db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "SELECT ?, id, ?, ? FROM users WHERE id = ? AND password_hash = ?",
            (token_hash(token), utc_ts(created), utc_ts(expires), user["id"], user["password_hash"]),
        )
    if cur.rowcount != 1:
        return False
    session.clear()
    session.permanent = True
    session["sid"] = token
    session["csrf"] = secrets.token_urlsafe(32)
    return True


@bp.route("/logout", methods=["POST"])
def logout():
    sid = session.get("sid")
    if isinstance(sid, str) and sid:
        db = get_db()
        with transaction(db):
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(sid),))
    session.clear()
    flash("You're logged out. Come back hungry!", "info")
    return redirect(url_for("auth.login"))
