import sqlite3

import pytest

from forkful.db import MigrationError, connect, migrate
from forkful.ids import new_public_id


def test_fresh_migrate_creates_schema_in_wal_mode(app, db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"users", "sessions", "recipes", "ingredients", "photos", "recipe_tags"} <= tables


def test_migrate_twice_is_a_no_op_and_keeps_data(app, users, db):
    old, applied = migrate(app.config["DATABASE"])
    assert (old, applied) == (6, [])
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_migrate_refuses_a_newer_database(app, db):
    db.execute("PRAGMA user_version = 99")
    with pytest.raises(MigrationError, match="99"):
        migrate(app.config["DATABASE"])


def _user_ids(db):
    return [r[0] for r in db.execute("SELECT id FROM users ORDER BY id")]


def _insert_recipe(db, user_id, parent_id=None, recipe_id=None, public_id=None):
    cur = db.execute(
        "INSERT INTO recipes (id, public_id, user_id, parent_id, name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'x', 't', 't')",
        (recipe_id, public_id or new_public_id(), user_id, parent_id),
    )
    return cur.lastrowid


def test_database_rejects_a_parent_owned_by_someone_else(app, users, db):
    alice_id, bob_id = _user_ids(db)
    alices = _insert_recipe(db, alice_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_recipe(db, bob_id, parent_id=alices)


def test_database_rejects_missing_parent_and_self_parent(app, users, db):
    alice_id, _ = _user_ids(db)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_recipe(db, alice_id, parent_id=12345)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_recipe(db, alice_id, parent_id=50, recipe_id=50)


def test_database_allows_root_and_same_owner_parent(app, users, db):
    alice_id, _ = _user_ids(db)
    root = _insert_recipe(db, alice_id)
    child = _insert_recipe(db, alice_id, parent_id=root)
    assert db.execute("SELECT parent_id FROM recipes WHERE id = ?", (child,)).fetchone()[0] == root


def test_database_requires_integer_calories(app, users, db):
    alice_id, _ = _user_ids(db)
    recipe = _insert_recipe(db, alice_id)
    insert = "INSERT INTO ingredients (recipe_id, position, name, calories) VALUES (?, ?, 'x', ?)"
    db.execute(insert, (recipe, 0, 5))
    for position, bad in enumerate([1.5, -1, 1_000_001, "twelve"], start=1):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (recipe, position, bad))


def test_database_rejects_bad_photo_keys(app, users, db):
    alice_id, _ = _user_ids(db)
    recipe = _insert_recipe(db, alice_id)
    insert = ("INSERT INTO photos (recipe_id, file_key, content_type, width, height, byte_size, position, created_at) "
              "VALUES (?, ?, 'image/png', 1, 1, 1, 0, 't')")
    for bad in ["../../etc/passwd", "A" * 32, "a" * 31, "g" * 32]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (recipe, bad))
    db.execute(insert, (recipe, "0123456789abcdef0123456789abcdef"))


def test_connections_enforce_foreign_keys(app):
    conn = connect(app.config["DATABASE"])
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_database_requires_a_well_formed_public_id(app, users, db):
    alice_id, _ = _user_ids(db)
    insert = "INSERT INTO recipes (public_id, user_id, name, created_at, updated_at) VALUES (?, ?, 'x', 't', 't')"
    for bad in [None, "", "a" * 11, "a" * 13, "A" * 12, "abc_def-ghij", "a" * 11 + "\x00", "é" * 12,
                12345, b"a" * 12, "0123456789abcdef0123456789abcdef"]:   # v4's 32-hex ids are gone too
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (bad, alice_id))
    db.execute(insert, ("x4k9m2q7w3rt", alice_id))
    with pytest.raises(sqlite3.IntegrityError):                      # unique, across all users
        db.execute(insert, ("x4k9m2q7w3rt", alice_id))


def test_a_public_id_never_changes(app, users, db):
    alice_id, _ = _user_ids(db)
    recipe = _insert_recipe(db, alice_id, public_id="a" * 12)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE recipes SET public_id = ? WHERE id = ?", ("b" * 12, recipe))
    db.execute("UPDATE recipes SET name = 'renamed', public_id = public_id WHERE id = ?", (recipe,))   # same value is fine
    assert db.execute("SELECT public_id FROM recipes WHERE id = ?", (recipe,)).fetchone()[0] == "a" * 12
