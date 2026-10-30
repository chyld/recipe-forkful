import os
import re
import stat

import pytest

from conftest import PASSWORDS, csrf_from_page, logged_in, login
from forkful import auth, create_app


def test_login_page_has_no_sign_up(app):
    client = app.test_client()
    html = client.get("/login").get_data(as_text=True)
    assert "sign up" not in html.lower() and "register" not in html.lower()
    for path in ["/signup", "/register", "/users/new"]:
        assert client.get(path).status_code in (302, 404)
    for path in ["/signup", "/register"]:
        assert client.post(path).status_code in (302, 404)


def test_login_and_logout(app, users):
    alice = logged_in(app, "alice")
    assert alice.get("/").status_code == 200
    response = alice.post("/logout")
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert alice.get("/").status_code == 302


def test_logout_really_ends_the_session(app, users, db):
    alice = logged_in(app, "alice")
    with alice.client.session_transaction() as sess:
        stolen = dict(sess)
    alice.post("/logout")
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    thief = app.test_client()
    with thief.session_transaction() as sess:
        sess.update(stolen)                      # replaying the old cookie contents
    assert thief.get("/").status_code == 302


def test_unknown_user_and_wrong_password_look_the_same(app, users, monkeypatch):
    calls = []
    real_verify = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda h, p: calls.append(h) or real_verify(h, p))

    unknown = login(app.test_client(), "nobody", "whatever-password")
    wrong = login(app.test_client(), "alice", "not-her-password")
    assert unknown.status_code == wrong.status_code == 200
    assert auth.LOGIN_FAILED.replace("'", "&#39;") in unknown.get_data(as_text=True)
    assert auth.LOGIN_FAILED.replace("'", "&#39;") in wrong.get_data(as_text=True)
    assert len(calls) == 2 and calls[0] == auth.dummy_hash()    # the unknown user still costs a verify


def test_garbage_login_input_is_rejected_cleanly(app, users):
    for username, password in [("x" * 500, "pw"), ("<script>", "pw"), ("alice", "p" * 5000), ("", ""), ("alice\x00", "pw")]:
        response = login(app.test_client(), username, password)
        assert response.status_code == 200
        assert "<script>" not in response.get_data(as_text=True)


def test_session_cookie_flags(app, users):
    client = app.test_client()
    response = login(client, "alice", PASSWORDS["alice"])
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Expires=" in cookie                  # a real 30-day cookie, not a browser-session one
    # Deliberately NOT Secure: the app is served over plain HTTP (PLAN.md §11).
    assert "Secure" not in cookie


def test_each_login_gets_a_fresh_token_and_csrf(app, users, db):
    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)
    anonymous_csrf = csrf_from_page(page)
    login(client, "alice", PASSWORDS["alice"])
    with client.session_transaction() as sess:
        first_sid, first_csrf = sess["sid"], sess["csrf"]
    assert first_csrf != anonymous_csrf
    client.post("/logout", data={"csrf_token": first_csrf})
    login(client, "alice", PASSWORDS["alice"])
    with client.session_transaction() as sess:
        assert sess["sid"] != first_sid
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_expired_session_is_logged_out(app, users, clock):
    alice = logged_in(app, "alice")
    clock.advance(days=30, seconds=1)
    assert alice.get("/").status_code == 302


def test_login_fails_if_password_changes_mid_login(app, users, runner, monkeypatch):
    real_verify = auth.verify_password

    def verify_then_change(password_hash, password):
        ok = real_verify(password_hash, password)
        runner.invoke(args=["set-password", "alice", "--password-stdin"], input="changed-meanwhile-1\n")
        return ok

    monkeypatch.setattr(auth, "verify_password", verify_then_change)
    client = app.test_client()
    response = login(client, "alice", PASSWORDS["alice"])
    assert response.status_code == 200                       # no session was minted
    assert client.get("/").status_code == 302


# --- throttle ------------------------------------------------------------------

def test_throttle_blocks_a_user_ip_pair_after_10_failures(app, users, clock):
    client = app.test_client()
    for _ in range(10):
        assert login(client, "alice", "wrong-password").status_code == 200
    assert login(client, "alice", PASSWORDS["alice"]).status_code == 429
    clock.advance(minutes=14)
    assert login(client, "alice", PASSWORDS["alice"]).status_code == 429
    clock.advance(minutes=1, seconds=1)
    assert login(client, "alice", PASSWORDS["alice"]).status_code == 302


def test_throttle_is_per_ip_so_others_can_still_log_in(app, users):
    attacker = app.test_client()
    for _ in range(10):
        login(attacker, "alice", "wrong-password")
    assert login(attacker, "alice", "wrong-password").status_code == 429
    victim = app.test_client()
    victim_login = victim.get("/login")
    token = csrf_from_page(victim_login.get_data(as_text=True))
    response = victim.post("/login", data={"csrf_token": token, "username": "alice", "password": PASSWORDS["alice"]},
                           environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert response.status_code == 302


def test_throttle_caps_one_ip_across_usernames(app, users):
    client = app.test_client()
    for i in range(50):
        login(client, f"user{i}", "wrong-password")
    assert login(client, "bob", PASSWORDS["bob"]).status_code == 429


def test_success_resets_the_pair(app, users):
    client = app.test_client()
    for _ in range(9):
        login(client, "alice", "wrong-password")
    assert login(client, "alice", PASSWORDS["alice"]).status_code == 302
    client.post("/logout", data={"csrf_token": _csrf(client)})
    for _ in range(9):
        assert login(client, "alice", "wrong-password").status_code == 200


def _csrf(client):
    with client.session_transaction() as sess:
        return sess.get("csrf", "")


def test_throttle_memory_is_bounded():
    from datetime import datetime, timedelta, timezone
    throttle = auth.LoginThrottle(timedelta(minutes=15), 10, 50, max_keys=100)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(1000):
        throttle.record_failure(f"10.0.{i // 250}.{i % 250}", f"user{i}", now)
    assert len(throttle._pairs.entries) <= 100 and len(throttle._ips.entries) <= 100


def test_throttle_keys_never_hold_huge_usernames():
    from datetime import datetime, timedelta, timezone
    throttle = auth.LoginThrottle(timedelta(minutes=15), 10, 50)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(20):
        throttle.record_failure("10.0.0.1", "x" * 1_000_000 + str(i), now)
    assert list(throttle._pairs.entries) == [("10.0.0.1", auth.INVALID_USERNAME_KEY)]


def test_username_flood_cannot_evict_a_blocked_ips_counter():
    from datetime import datetime, timedelta, timezone
    throttle = auth.LoginThrottle(timedelta(minutes=15), 10, 3, max_keys=5)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(3):
        throttle.record_failure("203.0.113.7", "alice", now)            # this IP is now blocked
    for i in range(100):                                                 # flood of distinct usernames
        throttle.record_failure("198.51.100.1", f"user{i}", now)
    # Per-pair and per-IP counters live apart, so the flood can't push the blocked IP out.
    assert throttle.blocked("203.0.113.7", "bob", now)


# --- secret key ------------------------------------------------------------------

def test_secret_key_file_is_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    first = create_app({"DATA_DIR": tmp_path})
    path = tmp_path / "secret_key"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    second = create_app({"DATA_DIR": tmp_path})
    assert first.config["SECRET_KEY"] == second.config["SECRET_KEY"]
    assert len(first.config["SECRET_KEY"]) >= 64
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".secret_key")] == []


def test_secret_key_env_override_must_be_strong(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    assert create_app({"DATA_DIR": tmp_path}).config["SECRET_KEY"] == "x" * 40
    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="32"):
        create_app({"DATA_DIR": tmp_path})


def test_non_ascii_csrf_token_is_a_400_not_a_500(app, users, alice):
    client = app.test_client()
    client.get("/login")
    assert client.post("/login", data={"csrf_token": "é", "username": "alice", "password": "x"}).status_code == 400
    assert alice.post("/recipes/new", data={"csrf_token": "ünïcödé", "name": "x"}, csrf=False).status_code == 400


def test_malformed_logins_count_toward_the_ip_limit(app, users):
    client = app.test_client()
    for i in range(10):
        login(client, "bad name " + str(i), "pw")       # invalid usernames share one bounded key...
    for i in range(40):
        login(client, f"user{i}", "pw")                  # ...but still count toward this IP's 50
    assert login(client, "alice", PASSWORDS["alice"]).status_code == 429


def test_first_login_ip_is_logged_at_info(app, users, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger=app.logger.name):
        login(app.test_client(), "alice", "wrong-password")
        login(app.test_client(), "alice", "wrong-password")
    messages = [r.getMessage() for r in caplog.records if "First login attempt" in r.getMessage()]
    assert len(messages) == 1 and "127.0.0.1" in messages[0]
    assert app.logger.getEffectiveLevel() <= logging.INFO


def test_sessions_last_30_days(app, users, clock):
    from datetime import datetime, timedelta, timezone
    from email.utils import parsedate_to_datetime
    client = app.test_client()
    response = login(client, "alice", PASSWORDS["alice"])
    expires = parsedate_to_datetime(re.search(r"Expires=([^;]+)", response.headers["Set-Cookie"]).group(1))
    assert abs(expires - (datetime.now(timezone.utc) + timedelta(days=30))) < timedelta(minutes=5)
    clock.advance(days=29)
    assert client.get("/").status_code == 200


def test_login_cleans_up_expired_sessions(app, users, db, clock):
    alice = logged_in(app, "alice")
    clock.advance(days=31)
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1   # expired, still stored
    logged_in(app, "bob")
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1   # alice's gone, bob's new one left


def test_oversized_urlencoded_body_is_refused_before_parsing(app, users):
    client = app.test_client()
    client.get("/login")
    response = client.post("/login", data={"username": "a", "password": "p", "pad": "x" * (300 * 1024)})
    assert response.status_code == 413
