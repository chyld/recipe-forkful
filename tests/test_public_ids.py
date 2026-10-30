"""Public recipe ids: random 12-character ids (0-9, a-z) in every URL and page; integer ids stay inside."""

import re
import sqlite3

import pytest
from flask import url_for

import factories
from conftest import assert_search_index_in_sync
from forkful import ids, repo
from forkful.db import MIGRATIONS_DIR, connect, migrate

PUBLIC_ID = re.compile(r"[0-9a-z]{12}")


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.get_data(as_text=True)


def test_new_recipes_and_forks_get_random_public_ids(alice, db):
    first = alice.create_recipe("One")
    second = alice.create_recipe("Two")
    child = alice.fork(first, "One remix").headers["Location"].rsplit("/", 1)[1]
    assert all(PUBLIC_ID.fullmatch(p) for p in (first, second, child))
    assert len({first, second, child}) == 3
    assert int(first, 36) + 1 != int(second, 36)                       # not a counter in disguise
    rows = db.execute("SELECT id, public_id FROM recipes ORDER BY id").fetchall()
    assert [r["public_id"] for r in rows] == [first, second, child]


def test_every_redirect_uses_the_public_id(alice, db):
    created = alice.post("/recipes/new", data={"name": "Soup"}, content_type="multipart/form-data")
    public_id = created.headers["Location"].rsplit("/", 1)[1]
    assert created.headers["Location"] == f"/recipes/{public_id}"
    assert alice.edit_recipe(public_id, name="Soup!").headers["Location"] == f"/recipes/{public_id}"
    forked = alice.fork(public_id, "Soup remix").headers["Location"]
    assert re.fullmatch(r"/recipes/[0-9a-z]{12}", forked) and public_id not in forked


def test_no_integer_ids_in_any_page(alice, db):
    parent = alice.create_recipe("Parent", [("Salt", "1", 1)], photos=[("a.png", factories.png()),
                                                                         ("b.png", factories.png())])
    child = alice.fork(parent, "Child").headers["Location"].rsplit("/", 1)[1]
    leaks = re.compile(r'/recipes/\d+(?=["/?#])|value="photo-\d+"|/photos/\d+/')
    for path in ["/", "/search", f"/recipes/{parent}", f"/recipes/{parent}/edit", f"/recipes/{parent}/fork",
                 f"/recipes/{child}"]:
        html = page(alice, path)
        assert not leaks.search(html), (path, leaks.search(html).group(0))
    links = re.findall(r'href="/recipes/([^"/]+)"', page(alice, f"/recipes/{child}"))
    assert parent in links                                              # the parent link uses its public id


def test_building_a_url_from_an_integer_fails_loudly(app):
    with app.test_request_context():
        assert url_for("recipes.show", public_id="x4k9m2q7w3rt") == "/recipes/x4k9m2q7w3rt"
        for bad in [8, "8", "X4K9M2Q7W3RT", "0123456789abcdef0123456789abcdef"]:
            with pytest.raises(ValueError):
                url_for("recipes.show", public_id=bad)


def test_a_colliding_public_id_is_retried(app, alice, db, monkeypatch):
    taken = alice.create_recipe("First")
    fresh = "fresh0000000"
    tries = iter([taken, fresh])
    monkeypatch.setattr(ids, "new_public_id", lambda: next(tries))
    assert alice.create_recipe("Second") == fresh


def test_it_gives_up_after_a_few_collisions(app, alice, db, monkeypatch):
    taken = alice.create_recipe("First")
    monkeypatch.setattr(ids, "new_public_id", lambda: taken)
    user_id = db.execute("SELECT user_id FROM recipes").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="recipes.public_id"):
        repo.insert_recipe(db, user_id, "Second", "", "t")


def test_migrating_a_v3_database_gives_every_recipe_a_public_id(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    for version in (1, 2, 3):
        conn.executescript(next(MIGRATIONS_DIR.glob(f"000{version}_*.sql")).read_text())
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', 't')")
    for name in ("Stew", "Bread", "Cake"):
        conn.execute("INSERT INTO recipes (user_id, name, instructions, created_at, updated_at) "
                     "VALUES (1, ?, 'Bake.', 't', 't')", (name,))
    conn.execute("INSERT INTO recipes (user_id, parent_id, name, created_at, updated_at) "
                 "VALUES (1, 1, 'Stew remix', 't', 't')")
    conn.execute("INSERT INTO ingredients (recipe_id, position, name) VALUES (1, 0, 'parsnips')")
    conn.execute("INSERT INTO recipe_tags (recipe_id, user_id, tag, position) VALUES (4, 1, 'winter', 0)")
    conn.close()

    assert migrate(path) == (3, [4, 5])
    conn = connect(path)
    public_ids = [r[0] for r in conn.execute("SELECT public_id FROM recipes ORDER BY id")]
    assert len(set(public_ids)) == 4 and all(PUBLIC_ID.fullmatch(p) for p in public_ids)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT rowid FROM recipe_text WHERE recipe_text MATCH 'parsnip'").fetchone()[0] == 1
    assert conn.execute("SELECT parent_id FROM recipes WHERE name = 'Stew remix'").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):                       # the rules hold from here on
        conn.execute("INSERT INTO recipes (user_id, name, created_at, updated_at) VALUES (1, 'x', 't', 't')")
    conn.close()
    assert_search_index_in_sync(path)


def test_migrating_a_v4_database_shortens_every_public_id(tmp_path):
    path = tmp_path / "v4.db"
    conn = connect(path)
    for version in (1, 2, 3, 4):
        conn.executescript(next(MIGRATIONS_DIR.glob(f"000{version}_*.sql")).read_text())
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', 't')")
    for i in range(50):
        conn.execute("INSERT INTO recipes (public_id, user_id, name, created_at, updated_at) "
                     "VALUES (lower(hex(randomblob(16))), 1, ?, 't', 't')", (f"Recipe {i}",))
    conn.execute("INSERT INTO ingredients (recipe_id, position, name) VALUES (7, 0, 'parsnips')")
    old_ids = {r[0] for r in conn.execute("SELECT public_id FROM recipes")}
    conn.close()

    assert migrate(path) == (4, [5])
    conn = connect(path)
    new_ids = [r[0] for r in conn.execute("SELECT public_id FROM recipes")]
    assert len(set(new_ids)) == 50 and all(PUBLIC_ID.fullmatch(p) for p in new_ids)
    assert not old_ids & set(new_ids)
    assert conn.execute("SELECT rowid FROM recipe_text WHERE recipe_text MATCH 'parsnip'").fetchone()[0] == 7
    with pytest.raises(sqlite3.IntegrityError):                       # 32 hex is no longer accepted
        conn.execute("INSERT INTO recipes (public_id, user_id, name, created_at, updated_at) "
                     "VALUES (?, 1, 'x', 't', 't')", ("0123456789abcdef0123456789abcdef",))
    with pytest.raises(sqlite3.IntegrityError):                       # and ids are fixed again
        conn.execute("UPDATE recipes SET public_id = 'zzzzzzzzzzzz' WHERE id = 1")
    conn.close()


def test_generated_ids_use_the_whole_alphabet():
    sample = "".join(ids.new_public_id() for _ in range(300))
    assert set(sample) == set(ids.PUBLIC_ID_ALPHABET)
    assert all(ids.PUBLIC_ID_RE.fullmatch(ids.new_public_id()) for _ in range(100))
