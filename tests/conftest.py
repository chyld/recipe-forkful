import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from forkful import create_app
from forkful.db import connect, migrate

KB = 1024
PASSWORDS = {"alice": "alice-password-123", "bob": "bob-password-456"}


class Clock:
    """A controllable clock. Ticks 1µs per read so timestamps never tie by accident."""

    def __init__(self):
        self.now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(microseconds=1)
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(tmp_path, clock, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    app = create_app({
        "TESTING": True,
        "DATA_DIR": tmp_path / "data",
        "CLOCK": clock,
        # Shrunken limits so the limit tests stay fast.
        "MAX_CONTENT_LENGTH": 3 * 1024 * KB,
        "MAX_PHOTO_BYTES": 600 * KB,
        "MAX_PHOTO_PIXELS": 60_000_000,
        "PHOTO_MAX_EDGE": 4096,
    })
    migrate(app.config["DATABASE"])
    yield app
    assert_search_index_in_sync(app.config["DATABASE"])   # after every test, whatever it wrote


def assert_search_index_in_sync(db_path):
    """The full-text index must hold exactly each recipe's name, ingredient names and
    instructions: computed here in Python from the base tables, not with the triggers' SQL."""
    db = connect(db_path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'recipe_text'").fetchone():
            return
        db.execute("INSERT INTO recipe_text (recipe_text) VALUES ('integrity-check')")
        names = {}
        for recipe_id, name in db.execute("SELECT recipe_id, name FROM ingredients"):
            names.setdefault(recipe_id, []).append(name)
        expected = {
            r["id"]: (r["name"], sorted(" ".join(names.get(r["id"], [])).split()), r["instructions"])
            for r in db.execute("SELECT id, name, instructions FROM recipes")
        }
        actual = {
            r["rowid"]: (r["name"], sorted(r["ingredients"].split()), r["instructions"])
            for r in db.execute("SELECT rowid, name, ingredients, instructions FROM recipe_text")
        }
        assert actual == expected, "the search index drifted from the recipes"
    finally:
        db.close()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


def make_user(runner, username, password=None):
    password = password or PASSWORDS.get(username, "some-password-1")
    result = runner.invoke(args=["create-user", username, "--password-stdin"], input=password + "\n")
    assert result.exit_code == 0, result.output
    return password


@pytest.fixture
def users(runner):
    for name in PASSWORDS:
        make_user(runner, name)
    return PASSWORDS


def csrf_from_page(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token on page"
    return match.group(1)


def session_csrf(client):
    with client.session_transaction() as sess:
        return sess.get("csrf")


def login(client, username, password):
    token = csrf_from_page(client.get("/login").get_data(as_text=True))
    return client.post("/login", data={"csrf_token": token, "username": username, "password": password})


class UserClient:
    """A logged-in test client that adds the CSRF token to every POST."""

    def __init__(self, client):
        self.client = client

    def get(self, path, **kwargs):
        return self.client.get(path, **kwargs)

    def post(self, path, data=None, csrf=True, **kwargs):
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf_token", session_csrf(self.client))
        return self.client.post(path, data=data, **kwargs)

    def open(self, *args, **kwargs):
        return self.client.open(*args, **kwargs)

    def create_recipe(self, name="Pancakes", ingredients=(), instructions="", photos=(), tags=""):
        data = recipe_data(name, ingredients, instructions, photos, tags)
        response = self.post("/recipes/new", data=data, content_type="multipart/form-data")
        assert response.status_code == 302, response.get_data(as_text=True)
        return response.headers["Location"].rsplit("/", 1)[1]

    def edit_recipe(self, recipe_id, name="Pancakes", ingredients=(), instructions="", photos=(), cover=None):
        data = recipe_data(name, ingredients, instructions, photos)
        if cover is not None:
            data["cover"] = cover
        return self.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data")

    def delete_photo(self, recipe_id, file_key, fetch=False):
        headers = {"X-Requested-With": "fetch"} if fetch else {}
        return self.post(f"/recipes/{recipe_id}/photos/{file_key}/delete", headers=headers)

    def fork(self, recipe_id, name):
        return self.post(f"/recipes/{recipe_id}/fork", data={"name": name})


def internal_id(db, public_id):
    """The integer id behind a recipe's public id (tests that look at tables need it)."""
    return db.execute("SELECT id FROM recipes WHERE public_id = ?", (public_id,)).fetchone()[0]


def recipe_data(name, ingredients=(), instructions="", photos=(), tags=""):
    data = {"name": name, "instructions": instructions, "tags": tags}
    for i, row in enumerate(ingredients):
        ing_name, amount, calories = (list(row) + ["", "", ""])[:3]
        data[f"ing-{i}-name"] = ing_name
        data[f"ing-{i}-amount"] = amount
        data[f"ing-{i}-calories"] = str(calories)
    data["photos"] = [(io.BytesIO(content), filename) for filename, content in photos]
    return data


def logged_in(app, username):
    client = app.test_client()
    response = login(client, username, PASSWORDS[username])
    assert response.status_code == 302, response.get_data(as_text=True)
    return UserClient(client)


@pytest.fixture
def alice(app, users):
    return logged_in(app, "alice")


@pytest.fixture
def bob(app, users):
    return logged_in(app, "bob")


@pytest.fixture
def db(app):
    conn = connect(app.config["DATABASE"])
    yield conn
    conn.close()


def photo_files(app):
    return sorted(p.name for p in app.config["PHOTOS_DIR"].iterdir())
