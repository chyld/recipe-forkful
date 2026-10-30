"""Recipe tags: lowercase a-z, no spaces; find recipes by tag on the search page."""

import re
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from conftest import internal_id, recipe_data
from forkful import forms
from forkful.db import connect, migrate


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.get_data(as_text=True)


def tagged(client, name, tags, **kwargs):
    data = recipe_data(name, **kwargs)
    data["tags"] = tags
    response = client.post("/recipes/new", data=data, content_type="multipart/form-data")
    assert response.status_code == 302, response.get_data(as_text=True)
    return response.headers["Location"].rsplit("/", 1)[1]


def tags_of(db, public_id):
    return [r[0] for r in db.execute(
        "SELECT t.tag FROM recipe_tags t JOIN recipes r ON r.id = t.recipe_id WHERE r.public_id = ? ORDER BY t.position",
        (public_id,))]


def card_titles(html):
    return re.findall(r'class="card-title">([^<]+)<', html)


# --- parsing ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("soup", ["soup"]),
    ("Soup, VEGAN", ["soup", "vegan"]),
    ("soup vegan  quick", ["soup", "vegan", "quick"]),
    (" soup,,, vegan , ", ["soup", "vegan"]),
    ("#soup #vegan", ["soup", "vegan"]),
    ("soup Soup SOUP vegan soup", ["soup", "vegan"]),
    ("soup\tvegan\nquick", ["soup", "vegan", "quick"]),
    ("", []),
    (" , , ", []),
    ("a" * 24, ["a" * 24]),
])
def test_forgiving_about_case_separators_and_repeats(raw, expected):
    assert forms.parse_tags(raw) == (expected, None)


@pytest.mark.parametrize("raw", [
    "one-pot", "soup2", "low_carb", "crème", "café", "a", "a" * 25, "so!up", "kK", "soup vegan",
    "ab\x00cd", "日本",
])
def test_strict_about_letters(raw):
    tags, error = forms.parse_tags(raw)
    assert tags == [] and "isn't a tag" in error


def test_limits():
    ten = " ".join(chr(97 + i) * 3 for i in range(10))
    assert len(forms.parse_tags(ten)[0]) == 10
    assert forms.parse_tags(ten + " zzz")[1] == "A recipe can have up to 10 tags."
    assert "300 characters" in forms.parse_tags("soup " * 61)[1]


@pytest.mark.parametrize("value, expected", [
    ("soup", "soup"), (" Soup ", "soup"), ("SOUP", "soup"),
    ("so up", None), ("soup1", None), ("a", None), ("kK", None), ("", None),
])
def test_canonical_tag(value, expected):
    assert forms.canonical_tag(value) == expected


# --- the database is the last line of defense ---------------------------------------------

def test_database_rejects_bad_tags(app, alice, db):
    recipe_id, user_id = db.execute("SELECT id, user_id FROM recipes WHERE public_id = ?",
                                    (alice.create_recipe("Soup"),)).fetchone()
    insert = "INSERT INTO recipe_tags (recipe_id, user_id, tag, position) VALUES (?, ?, ?, ?)"
    for position, bad in enumerate(["Soup", "soup1", "one-pot", "a", "a" * 25, "ab\x00cd", "café", "so up"]):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (recipe_id, user_id, bad, position))
    for position in range(10):
        db.execute(insert, (recipe_id, user_id, "tag" + "abcdefghij"[position], position))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, (recipe_id, user_id, "eleventh", 10))          # at most 10


def test_database_rejects_a_tag_on_someone_elses_recipe(app, alice, bob, db):
    alice_recipe = internal_id(db, alice.create_recipe("Alice's"))
    bob_id = db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO recipe_tags (recipe_id, user_id, tag, position) VALUES (?, ?, 'soup', 0)",
                   (alice_recipe, bob_id))


def test_migrating_an_existing_v1_database_keeps_everything(tmp_path):
    from forkful.db import MIGRATIONS_DIR
    path = tmp_path / "old.db"
    conn = connect(path)
    conn.executescript((MIGRATIONS_DIR / "0001_init.sql").read_text())
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', 't')")
    conn.execute("INSERT INTO recipes (user_id, name, created_at, updated_at) VALUES (1, 'Old soup', 't', 't')")
    conn.close()
    assert migrate(path) == (1, [2, 3, 4, 5, 6])
    conn = connect(path)
    assert conn.execute("SELECT name FROM recipes").fetchone()[0] == "Old soup"
    assert conn.execute("SELECT COUNT(*) FROM recipe_tags").fetchone()[0] == 0
    conn.close()


# --- create, edit, fork -------------------------------------------------------------------

def test_tags_are_saved_normalized_in_typed_order(alice, db):
    recipe_id = tagged(alice, "Chili", "Spicy, #weeknight  spicy beef")
    assert tags_of(db, recipe_id) == ["spicy", "weeknight", "beef"]
    html = page(alice, f"/recipes/{recipe_id}")
    for t in ["spicy", "weeknight", "beef"]:
        assert f'href="/search?tag={t}"' in html


def test_edit_replaces_and_can_clear_tags(alice, db):
    recipe_id = tagged(alice, "Chili", "spicy beef")
    edit = page(alice, f"/recipes/{recipe_id}/edit")
    assert 'value="spicy, beef"' in edit
    data = recipe_data("Chili"); data["tags"] = "vegan"
    alice.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data")
    assert tags_of(db, recipe_id) == ["vegan"]
    data["tags"] = ""
    alice.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data")
    assert tags_of(db, recipe_id) == []


def test_bad_tags_re_render_with_what_was_typed(alice, db):
    data = recipe_data("Soup"); data["tags"] = "soup, one-pot"
    response = alice.post("/recipes/new", data=data, content_type="multipart/form-data")
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "“one-pot” isn't a tag." in html.replace("&#39;", "'")
    assert 'value="soup, one-pot"' in html and 'aria-invalid="true"' in html
    assert db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0


def test_a_failed_edit_keeps_the_old_tags(alice, db):
    recipe_id = tagged(alice, "Soup", "warm")
    data = recipe_data(""); data["tags"] = "cold"                     # invalid: no name
    assert alice.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data").status_code == 400
    assert tags_of(db, recipe_id) == ["warm"]


def test_forks_copy_tags_and_then_go_their_own_way(alice, db):
    parent = tagged(alice, "Pancakes", "breakfast sweet")
    response = alice.fork(parent, "Savory pancakes")
    child = response.headers["Location"].rsplit("/", 1)[1]
    assert tags_of(db, child) == ["breakfast", "sweet"]
    data = recipe_data("Savory pancakes"); data["tags"] = "breakfast savory"
    alice.post(f"/recipes/{child}/edit", data=data, content_type="multipart/form-data")
    assert tags_of(db, child) == ["breakfast", "savory"]
    assert tags_of(db, parent) == ["breakfast", "sweet"]
    assert "and tags" in page(alice, f"/recipes/{parent}/fork")


# --- finding recipes by tag ------------------------------------------------------------------

def test_filter_by_one_tag_freshest_first(alice, clock):
    tagged(alice, "Tomato soup", "soup vegan")
    clock.advance(minutes=1)
    tagged(alice, "Chili", "spicy")
    clock.advance(minutes=1)
    tagged(alice, "Miso soup", "soup")
    html = page(alice, "/search?tag=soup")
    assert card_titles(html) == ["Miso soup", "Tomato soup"]
    assert "2 recipes with" in html and ">Clear search</a>" in html
    assert card_titles(page(alice, "/")) == ["Miso soup", "Chili", "Tomato soup"]


def test_tag_bar_is_a_checkbox_form_with_totals(alice):
    tagged(alice, "A", "soup vegan"); tagged(alice, "B", "soup"); tagged(alice, "C", "breakfast")
    html = page(alice, "/search")
    form = html[html.index('class="search-form"'):html.index("</form>", html.index('class="search-form"'))]
    assert 'method="get"' in form and 'action="/search"' in form
    assert re.findall(r'name="tag" value="(\w+)"', form) == ["breakfast", "soup", "vegan"]
    assert re.search(r'soup</span>\s*<span class="tag-count"><span class="sr-only">, </span>2<', form)
    assert 'type="submit"' in form and "Search" in form
    assert "<legend" in form and "every tag you pick" in form
    assert re.search(r'class="tag-chip tag-chip-all" href="/search" aria-current="true"', form)
    searched = page(alice, "/search?tag=soup&tag=vegan")
    ticked = re.findall(r'name="tag" value="(\w+)" checked', searched)
    assert ticked == ["soup", "vegan"]                                 # the form shows what was searched
    assert 'aria-current="true"' not in searched                        # "All" is no longer current
    # totals stay totals while searching (they don't change with the selection)
    assert re.search(r'breakfast</span>\s*<span class="tag-count"><span class="sr-only">, </span>1<', searched)


def test_without_tags_the_search_page_still_sorts(alice):
    alice.create_recipe("Plain")
    html = page(alice, "/search")
    assert "No tags yet" in html and 'name="tag"' not in html
    assert 'name="sort"' in html and card_titles(html) == ["Plain"]


def test_non_canonical_searches_redirect_to_one_sorted_url(alice):
    tagged(alice, "Soup", "soup vegan")
    for path in ["/search?tag=Soup", "/search?tag=%20soup%20", "/search?tag=soup&tag=soup"]:
        response = alice.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/search?tag=soup"), path
    for path in ["/search?tag=vegan&tag=soup", "/search?tag=VEGAN&tag=soup&tag=vegan", "/search?tag=&tag=vegan&tag=soup"]:
        response = alice.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/search?tag=soup&tag=vegan"), path
    assert alice.get("/search?tag=soup&tag=vegan").status_code == 200        # canonical: no redirect loop
    assert alice.get("/search?tag=soup").status_code == 200                  # old one-tag links still work


def test_unknown_and_invalid_tags_are_friendly_and_never_echoed(alice):
    tagged(alice, "Soup", "soup")
    unknown = page(alice, "/search?tag=pizza")
    assert "No recipes tagged" in unknown and ">pizza<" in unknown and card_titles(unknown) == []
    invalid = page(alice, "/search?tag=%3Cscript%3Ealert(1)%3C%2Fscript%3E")
    assert "That isn't a tag" in invalid.replace("&#39;", "'") and card_titles(invalid) == []
    assert "<script>alert" not in invalid and "alert(1)" not in invalid
    empty = alice.get("/search?tag=")                                    # empty value = no search
    assert empty.status_code == 302 and empty.headers["Location"] == "/search"


def test_tags_are_private(alice, bob):
    tagged(alice, "Alice's secret stew", "secretstew soup")
    tagged(bob, "Bob's soup", "soup")
    bob_home = page(bob, "/search")
    assert "secretstew" not in bob_home and "Alice" not in bob_home
    assert re.search(r'soup</span>\s*<span class="tag-count"><span class="sr-only">, </span>1<', bob_home)
    bob_secret = page(bob, "/search?tag=secretstew")
    bob_never = page(bob, "/search?tag=neverused")
    strip = lambda h: h.replace("secretstew", "X").replace("neverused", "X")
    assert strip(bob_secret) == strip(bob_never)                      # someone else's tag looks just like a made-up one
    assert card_titles(page(bob, "/search?tag=soup")) == ["Bob&#39;s soup"]


def test_cards_show_tags_as_labels_not_links(alice):
    tagged(alice, "Many", "one two three four five")
    html = page(alice, "/")
    card = html[html.index('class="recipe-card'):html.index("</a>", html.index('class="recipe-card'))]
    assert re.findall(r'class="tag-pill[^"]*">(\w+)<', card) == ["one", "two", "three"]
    assert "+3" in card and "+2" in card                                 # phone shows 2 (+3), wide shows 3 (+2)
    assert card.count("<a ") == 0                                        # no links nested in the card link


def test_tags_field_is_phone_friendly(alice):
    html = page(alice, "/recipes/new")
    field = re.search(r'<input id="tags"[^>]*>', html, re.S).group(0)
    for attr in ['autocapitalize="none"', 'autocorrect="off"', 'spellcheck="false"', 'maxlength="300"']:
        assert attr in field


# --- searching with several tags (v1.5) ---------------------------------------------------

def test_several_tags_match_recipes_having_all_of_them(alice, clock):
    tagged(alice, "Vegan soup", "soup vegan")
    clock.advance(minutes=1)
    tagged(alice, "Beef soup", "soup")
    clock.advance(minutes=1)
    tagged(alice, "Vegan chili", "vegan spicy")
    clock.advance(minutes=1)
    tagged(alice, "Spicy vegan soup", "soup spicy vegan")
    assert card_titles(page(alice, "/search?tag=soup&tag=vegan")) == ["Spicy vegan soup", "Vegan soup"]
    assert card_titles(page(alice, "/search?tag=soup&tag=spicy&tag=vegan")) == ["Spicy vegan soup"]
    html = page(alice, "/search?tag=soup&tag=vegan")
    assert "2 recipes with all of" in html


def test_no_recipe_has_all_the_tags(alice):
    tagged(alice, "Soup", "soup"); tagged(alice, "Cake", "dessert")
    html = page(alice, "/search?tag=dessert&tag=soup")
    assert card_titles(html) == []
    assert "No recipes have all of" in html and "Try removing one" in html


def test_each_picked_tag_can_be_removed_on_its_own(alice):
    tagged(alice, "A", "soup vegan spicy")
    html = page(alice, "/search?tag=soup&tag=spicy&tag=vegan")
    tray = html[html.index('class="search-tray"'):html.index("</ul>", html.index('class="search-tray"'))]
    hrefs = re.findall(r'class="tag-remove" href="([^"]+)"', tray)
    assert hrefs == ["/search?tag=spicy&amp;tag=vegan", "/search?tag=soup&amp;tag=vegan", "/search?tag=soup&amp;tag=spicy"]
    assert 'class="clear-filter" href="/search">Clear search' in tray
    one = page(alice, "/search?tag=soup")
    assert re.search(r'class="tag-remove" href="/search"', one)               # removing the last tag clears the search


def test_a_bad_tag_spoils_the_whole_search_but_keeps_your_picks(alice):
    tagged(alice, "Soup", "soup vegan")
    html = page(alice, "/search?tag=soup&tag=vegan&tag=so%20up")
    assert "That isn't a tag" in html.replace("&#39;", "'")
    assert card_titles(html) == []                                     # no partial search
    assert "so up" not in html and "so%20up" not in html                # never echoed
    assert re.findall(r'name="tag" value="(\w+)" checked', html) == ["soup", "vegan"]


def test_at_most_ten_tags_per_search(alice):
    eleven = [chr(97 + i) * 3 for i in range(11)]
    tagged(alice, "Many", " ".join(eleven[:10]))
    ten = "&".join(f"tag={t}" for t in eleven[:10])
    assert card_titles(page(alice, "/search?" + ten)) == ["Many"]
    html = page(alice, "/search?" + ten + "&tag=" + eleven[10])
    assert "Pick up to 10 tags" in html and card_titles(html) == []
    flood = page(alice, "/search?" + "&".join(["tag=soup"] * 40))
    assert "That isn't a tag" in flood.replace("&#39;", "'")           # too many values: refused outright


def test_multi_tag_search_is_private(alice, bob):
    tagged(alice, "Alice's vegan soup", "soup vegan secretstew")
    tagged(bob, "Bob soup", "soup vegan")
    assert card_titles(page(bob, "/search?tag=soup&tag=vegan")) == ["Bob soup"]
    a = page(bob, "/search?tag=secretstew&tag=soup")
    b = page(bob, "/search?tag=neverused&tag=soup")
    assert a.replace("secretstew", "X").replace("neverused", "X").replace("X</span> + <span class=\"tag-word\">soup", "") == \
           b.replace("secretstew", "X").replace("neverused", "X").replace("X</span> + <span class=\"tag-word\">soup", "")


def test_tag_search_sql_uses_bound_parameters(app, alice, db):
    from forkful import repo
    tagged(alice, "Soup", "soup vegan")
    user_id = db.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()[0]
    evil = "soup') OR 1=1 --"
    assert repo.list_recipes(db, user_id, tags=[evil]) == []           # treated as a value, not SQL
    assert len(repo.list_recipes(db, user_id, tags=["soup", "vegan"])) == 1


def test_page_title_names_the_search(alice):
    tagged(alice, "Soup", "soup vegan")
    assert "<title>Recipes tagged soup + vegan · Forkful</title>" in page(alice, "/search?tag=soup&tag=vegan")
