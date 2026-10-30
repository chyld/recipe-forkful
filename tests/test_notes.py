"""A free-text note on each ingredient ("finely chopped"), v1.13.0 (PLAN.md §30)."""

import re
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from conftest import assert_search_index_in_sync, recipe_data
from forkful import forms
from forkful.db import MIGRATIONS_DIR, connect, migrate

NOTE = '<span class="ingredient-notes">{}</span>'


def form(*rows):
    data = MultiDict([("name", "Soup")])
    for i, fields in enumerate(rows):
        for key, value in fields.items():
            data.add(f"ing-{i}-{key}", value)
    return forms.parse_recipe_form(data)


# --- the form --------------------------------------------------------------------------

def test_notes_are_saved_with_their_ingredient():
    parsed = form({"name": "Onion", "amount": "1", "calories": "40", "notes": "finely chopped"}, {"name": "Salt"})
    assert parsed.ingredients() == [(0, "Onion", "1", 40, "finely chopped"), (1, "Salt", "", 0, "")]


def test_notes_are_one_tidy_line():
    # Like names and amounts: control characters (newlines too) are dropped and the ends trimmed.
    parsed = form({"name": "Onion", "notes": "  finely\u0007 chopped\n or grated \t"})
    assert parsed.ingredients()[0][4] == "finely chopped or grated"


def test_a_note_alone_still_needs_a_name():
    parsed = form({"notes": "to taste"})
    assert parsed.rows[0].errors == {"name": "Give this ingredient a name."}
    assert parsed.error_summary() == ["Ingredient 1: Give this ingredient a name."]


def test_notes_have_a_limit():
    assert not form({"name": "Onion", "notes": "n" * 300}).has_errors
    parsed = form({"name": "Onion", "notes": "n" * 301})
    assert parsed.rows[0].errors == {"notes": "Notes can be up to 300 characters."}


def test_a_form_without_notes_fields_still_works():
    # An edit page opened before the upgrade, submitted after it, has no ing-N-notes fields.
    assert form({"name": "Onion", "amount": "1"}).ingredients() == [(0, "Onion", "1", 0, "")]


# --- the database ------------------------------------------------------------------------

def test_the_database_holds_notes_to_their_limit(app, alice, db):
    alice.create_recipe("Soup", [("Leek", "2", 50)])
    insert = "INSERT INTO ingredients (recipe_id, position, name, notes) VALUES (1, ?, 'x', ?)"
    db.execute(insert, (5, "n" * 300))
    for position, bad in [(6, "n" * 301), (7, None)]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (position, bad))


def test_migrating_a_v5_database_gives_every_ingredient_empty_notes(tmp_path):
    path = tmp_path / "v5.db"
    conn = connect(path)
    for version in (1, 2, 3, 4, 5):
        conn.executescript(next(MIGRATIONS_DIR.glob(f"000{version}_*.sql")).read_text())
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', 't')")
    conn.execute("INSERT INTO recipes (public_id, user_id, name, created_at, updated_at) "
                 "VALUES ('abcdefabcdef', 1, 'Leek soup', 't', 't')")
    conn.execute("INSERT INTO ingredients (recipe_id, position, name, amount, calories) VALUES (1, 0, 'Leeks', '2', 50)")
    conn.close()

    assert migrate(path) == (5, [6])
    conn = connect(path)
    assert [tuple(r) for r in conn.execute("SELECT name, amount, calories, notes FROM ingredients")] == [
        ("Leeks", "2", 50, "")]
    assert conn.execute("SELECT rowid FROM recipe_text WHERE recipe_text MATCH 'leek'").fetchone()[0] == 1
    conn.close()
    assert_search_index_in_sync(path)


# --- the pages ---------------------------------------------------------------------------

def test_the_recipe_page_shows_notes_under_the_name(alice):
    public_id = alice.create_recipe("Soup", [("Onion", "1", 40, "finely chopped"), ("Salt", "", 0)])
    html = alice.get(f"/recipes/{public_id}").get_data(as_text=True)
    assert f'<span class="ingredient-name">Onion{NOTE.format("finely chopped")}</span>' in html
    assert '<span class="ingredient-name">Salt</span>' in html and html.count("ingredient-notes") == 1


def test_the_edit_form_shows_and_saves_notes(alice):
    public_id = alice.create_recipe("Soup", [("Onion", "1", 40, "finely chopped")])
    html = alice.get(f"/recipes/{public_id}/edit").get_data(as_text=True)
    assert re.search(r'<input id="ing-0-notes" name="ing-0-notes" type="text" value="finely chopped"', html)
    assert alice.edit_recipe(public_id, "Soup", [("Onion", "1", 40, "or shallots")]).status_code == 302
    assert NOTE.format("or shallots") in alice.get(f"/recipes/{public_id}").get_data(as_text=True)
    assert alice.edit_recipe(public_id, "Soup", [("Onion", "1", 40, "")]).status_code == 302   # cleared
    assert "ingredient-notes" not in alice.get(f"/recipes/{public_id}").get_data(as_text=True)


def test_every_form_row_has_a_notes_field(alice):
    html = alice.get("/recipes/new").get_data(as_text=True)
    assert len(re.findall(r'name="ing-\d+-notes"', html)) == 5
    assert 'name="ing-__I__-notes"' in html                              # rows added by JavaScript too
    assert 'placeholder="Finely chopped"' in html


def test_a_rejected_save_keeps_the_typed_notes(alice):
    data = recipe_data("Soup", [("Onion", "1", "lots", "finely chopped")])
    response = alice.post("/recipes/new", data=data, content_type="multipart/form-data")
    html = response.get_data(as_text=True)
    assert response.status_code == 400 and 'value="finely chopped"' in html


def test_a_too_long_note_is_explained(alice):
    data = recipe_data("Soup", [("Onion", "1", 40, "n" * 301)])
    response = alice.post("/recipes/new", data=data, content_type="multipart/form-data")
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Ingredient 1: Notes can be up to 300 characters." in html
    assert 'aria-invalid="true" aria-describedby="ing-0-notes-error"' in html and 'id="ing-0-notes-error"' in html


def test_notes_are_text_not_html(alice):
    public_id = alice.create_recipe("Soup", [("Onion", "1", 40, '"><script>alert(1)</script>')])
    show = alice.get(f"/recipes/{public_id}").get_data(as_text=True)
    edit = alice.get(f"/recipes/{public_id}/edit").get_data(as_text=True)
    assert "<script>alert(1)" not in show and "<script>alert(1)" not in edit
    assert NOTE.format("&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;") in show
    assert 'value="&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in edit


def test_a_fork_copies_the_notes(alice):
    parent = alice.create_recipe("Soup", [("Onion", "1", 40, "finely chopped")])
    child = alice.fork(parent, "Soup remix").headers["Location"]
    assert NOTE.format("finely chopped") in alice.get(child).get_data(as_text=True)


def test_the_downloaded_file_has_the_notes(alice):
    public_id = alice.create_recipe("Soup", [("Onion", "1", 40, "finely chopped")])
    html = alice.get(f"/recipes/{public_id}/download").get_data(as_text=True)
    assert f'<span class="ingredient-name">Onion{NOTE.format("finely chopped")}</span>' in html
