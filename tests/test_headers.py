import re

from forkful import recipes

EXPECTED_CSP = (
    "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def assert_security_headers(response):
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"


def test_headers_on_pages(app, alice):
    for response in [app.test_client().get("/login"), alice.get("/"), alice.get("/recipes/new")]:
        assert_security_headers(response)
        assert response.headers["Cache-Control"] == "no-store"


def test_headers_on_404(alice):
    response = alice.get("/nope")
    assert response.status_code == 404
    assert_security_headers(response)
    assert response.headers["Cache-Control"] == "no-store"
    assert "Nothing cooking here" in response.get_data(as_text=True)


def test_500_is_friendly_and_hides_the_traceback(alice, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("secret internals: /data/forkful.db")

    monkeypatch.setattr(recipes.repo, "list_recipes", explode)
    response = alice.get("/")
    assert response.status_code == 500
    assert_security_headers(response)
    assert response.headers["Cache-Control"] == "no-store"
    html = response.get_data(as_text=True)
    assert "Something burned" in html
    assert "Traceback" not in html and "secret internals" not in html


def test_405_is_friendly(alice):
    response = alice.get("/logout")
    assert response.status_code == 405
    assert "POST" in response.headers["Allow"]
    assert_security_headers(response)
    assert response.headers["Cache-Control"] == "no-store"


def test_static_files_are_cacheable(app):
    response = app.test_client().get("/static/css/forkful.css")
    assert response.status_code == 200
    assert "max-age=3600" in response.headers["Cache-Control"]
    assert_security_headers(response)


def test_footer_shows_the_version_on_every_page(app, alice):
    from forkful import __version__
    for response in [app.test_client().get("/login"), alice.get("/"), alice.get("/nope")]:
        assert f"Forkful v{__version__}" in response.get_data(as_text=True)
    assert "development build" in alice.get("/").get_data(as_text=True)


def test_footer_shows_the_build_stamp_when_there_is_one(tmp_path, monkeypatch):
    from forkful import create_app
    monkeypatch.delenv("SECRET_KEY", raising=False)
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path, "BUILD": "2026-10-29 22:58 UTC"})
    assert "built 2026-10-29 22:58 UTC" in app.test_client().get("/login").get_data(as_text=True)


def test_static_urls_carry_a_cache_busting_token(app, alice):
    html = alice.get("/").get_data(as_text=True)
    for asset in ["css/forkful.css", "js/forkful.js", "favicon.svg"]:
        assert re.search(rf'/static/{re.escape(asset)}\?v=[^"]+"', html), asset


def test_the_token_is_the_build_stamp_in_docker(tmp_path, monkeypatch):
    from forkful import create_app
    monkeypatch.delenv("SECRET_KEY", raising=False)
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path, "BUILD": "2026-10-29 23:10 UTC"})
    html = app.test_client().get("/login").get_data(as_text=True)
    assert "/static/css/forkful.css?v=2026-10-29+23:10+UTC" in html or "/static/css/forkful.css?v=2026-10-29%2023:10%20UTC" in html
