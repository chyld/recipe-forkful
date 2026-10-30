"""Word search (v1.7): /search?q= over names, ingredient names and instructions."""

import re
import sqlite3

import pytest

from conftest import assert_search_index_in_sync, recipe_data
from forkful import forms, repo
from forkful.db import MIGRATIONS_DIR, connect, migrate, transaction


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.get_data(as_text=True)


def redirects_to(client, path):
    response = client.get(path)
    assert response.status_code == 302, (path, response.status_code)
    return response.headers["Location"]


def card_titles(html):
    return re.findall(r'class="card-title">([^<]+)<', html)


def found(client, query):
    return card_titles(page(client, "/search?q=" + query))


def cook(client, name, ingredients=(), instructions="", tags=""):
    data = recipe_data(name, [(i, "2 cups", 10) for i in ingredients], instructions)
    data["tags"] = tags
    response = client.post("/recipes/new", data=data, content_type="multipart/form-data")
    assert response.status_code == 302, response.get_data(as_text=True)
    return response.headers["Location"].rsplit("/", 1)[1]


def edit(client, recipe_id, name, ingredients=(), instructions="", tags=""):
    data = recipe_data(name, [(i, "1", 10) for i in ingredients], instructions)
    data["tags"] = tags
    response = client.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data")
    assert response.status_code == 302, response.get_data(as_text=True)


@pytest.fixture
def pantry(alice, clock):
    cook(alice, "Shakshuka", ["eggs", "red peppers"], "Simmer the sauce, then crack in the eggs.", tags="breakfast")
    clock.advance(minutes=1)
    cook(alice, "Tomato soup", ["tomatoes", "garlic", "cream"], "Roast the garlic. Blend until smooth.", tags="soup")
    clock.advance(minutes=1)
    cook(alice, "Garlic bread", ["baguette", "butter"], "Bake until golden.", tags="baking")
    clock.advance(minutes=1)
    cook(alice, "Crème brûlée", ["cream", "sugar"], "Torch the sugar.", tags="dessert")
    return alice


# --- what matches ----------------------------------------------------------------------------

def test_words_match_names_ingredients_and_instructions(pantry):
    assert found(pantry, "shakshuka") == ["Shakshuka"]                      # name
    assert found(pantry, "baguette") == ["Garlic bread"]                    # ingredient
    assert found(pantry, "torch") == ["Crème brûlée"]                       # instructions
    assert found(pantry, "garlic") == ["Garlic bread", "Tomato soup"]       # name or ingredient, freshest first


def test_amounts_are_not_searched(pantry):
    assert found(pantry, "cups") == []


def test_every_word_must_match_across_fields(pantry):
    assert found(pantry, "garlic+blend") == ["Tomato soup"]                 # ingredient + instructions
    assert found(pantry, "garlic+torch") == []


def test_word_endings_accents_and_case_do_not_matter(pantry):
    assert found(pantry, "egg") == ["Shakshuka"]
    assert found(pantry, "tomato") == ["Tomato soup"]                       # "tomatoes" is stored
    assert found(pantry, "roasted") == ["Tomato soup"]
    assert found(pantry, "creme+brulee") == ["Crème brûlée"]
    assert found(pantry, "SIMMER") == ["Shakshuka"]


def test_three_letters_or_more_also_match_the_start_of_a_word(pantry):
    assert found(pantry, "toma") == ["Tomato soup"]
    assert found(pantry, "bag") == ["Garlic bread"]
    assert found(pantry, "re") == []                                         # two letters: whole word only


def test_little_words_are_ignored(pantry):
    assert found(pantry, "garlic+and+the+tomatoes") == ["Tomato soup"]


def test_words_combine_with_tags_and_sort(pantry, alice):
    cook(alice, "Cream tea", ["cream", "scones"], tags="baking")
    assert found(pantry, "cream") == ["Cream tea", "Crème brûlée", "Tomato soup"]
    assert card_titles(page(pantry, "/search?q=cream&tag=baking")) == ["Cream tea"]
    # 20, 20 and 30 calories: the tie goes to the fresher recipe
    assert card_titles(page(pantry, "/search?q=cream&sort=cal-asc")) == ["Cream tea", "Crème brûlée", "Tomato soup"]


# --- the index follows every change --------------------------------------------------------------

def test_edits_replace_the_old_words(pantry, db):
    recipe_id = db.execute("SELECT public_id FROM recipes WHERE name = 'Garlic bread'").fetchone()[0]
    edit(pantry, recipe_id, "Cheese toast", ["cheddar", "bread"], "Grill until bubbling.")
    assert found(pantry, "baguette") == [] and found(pantry, "golden") == []
    assert found(pantry, "cheddar") == found(pantry, "bubbling") == found(pantry, "cheese") == ["Cheese toast"]


def test_removing_every_ingredient_removes_their_words(pantry, db):
    recipe_id = db.execute("SELECT public_id FROM recipes WHERE name = 'Garlic bread'").fetchone()[0]
    edit(pantry, recipe_id, "Garlic bread", [], "Bake until golden.")
    assert found(pantry, "baguette") == [] and found(pantry, "golden") == ["Garlic bread"]


def test_forks_are_searchable_and_go_their_own_way(pantry, db):
    parent = db.execute("SELECT public_id FROM recipes WHERE name = 'Tomato soup'").fetchone()[0]
    response = pantry.fork(parent, "Smoky tomato soup")
    child = response.headers["Location"].rsplit("/", 1)[1]
    assert found(pantry, "blend") == ["Smoky tomato soup", "Tomato soup"]
    edit(pantry, child, "Smoky tomato soup", ["tomatoes", "chipotle"], "Char the peppers.")
    assert found(pantry, "chipotle") == ["Smoky tomato soup"]
    assert found(pantry, "garlic") == ["Garlic bread", "Tomato soup"]


def test_a_photo_change_does_not_reindex(pantry, db):
    before = db.execute("SELECT count(*) FROM recipe_text").fetchone()[0]
    recipe_id = db.execute("SELECT id FROM recipes WHERE name = 'Shakshuka'").fetchone()[0]
    db.execute("UPDATE recipes SET updated_at = 'later' WHERE id = ?", (recipe_id,))
    assert db.execute("SELECT count(*) FROM recipe_text").fetchone()[0] == before


def test_triggers_keep_raw_sql_writes_in_sync_including_deletes(pantry, db):
    """No route deletes recipes today, but the index must follow any write, however it's made."""
    soup = db.execute("SELECT id FROM recipes WHERE name = 'Tomato soup'").fetchone()[0]
    db.execute("UPDATE ingredients SET name = 'basil' WHERE recipe_id = ? AND name = 'garlic'", (soup,))
    db.execute("UPDATE recipes SET instructions = 'Stir.' WHERE id = ?", (soup,))
    assert found(pantry, "basil") == ["Tomato soup"] and found(pantry, "blend") == []
    assert_search_index_in_sync(pantry_db_path(db))
    db.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (soup,))
    db.execute("DELETE FROM recipes WHERE id = ?", (soup,))                 # ingredients cascade
    assert db.execute("SELECT count(*) FROM recipe_text WHERE rowid = ?", (soup,)).fetchone()[0] == 0
    assert found(pantry, "basil") == []


def test_a_rolled_back_save_leaves_the_index_alone(app, pantry, db):
    user_id = db.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()[0]
    recipe_id = db.execute("SELECT id FROM recipes WHERE name = 'Shakshuka'").fetchone()[0]
    with pytest.raises(RuntimeError):
        with transaction(db):
            repo.replace_ingredients(db, user_id, recipe_id, [(0, "anchovies", "", 0)])
            raise RuntimeError("boom")
    assert found(pantry, "anchovies") == [] and found(pantry, "peppers") == ["Shakshuka"]


def test_migrating_a_v2_database_indexes_existing_recipes(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    for version in (1, 2):
        conn.executescript(next(MIGRATIONS_DIR.glob(f"000{version}_*.sql")).read_text())
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('old', 'x', 't')")
    conn.execute("INSERT INTO recipes (user_id, name, instructions, created_at, updated_at) "
                 "VALUES (1, 'Old stew', 'Braise slowly.', 't', 't')")
    conn.execute("INSERT INTO ingredients (recipe_id, position, name) VALUES (1, 0, 'parsnips')")
    conn.close()
    assert migrate(path) == (2, [3, 4, 5])
    conn = connect(path)
    assert conn.execute("SELECT rowid FROM recipe_text WHERE recipe_text MATCH 'parsnip'").fetchall()[0][0] == 1
    conn.close()
    assert_search_index_in_sync(path)


# --- privacy and safety --------------------------------------------------------------------------

def test_words_never_find_someone_elses_recipes(pantry, bob):
    cook(bob, "Bob's secret saffron rice", ["saffron", "garlic"])
    assert found(bob, "garlic") == ["Bob&#39;s secret saffron rice"]
    assert found(pantry, "saffron") == []
    assert "Bob" not in page(pantry, "/search?q=saffron+garlic")


@pytest.mark.parametrize("hostile", [
    '"', "*", "garlic*", "NEAR(garlic+cream)", "garlic+OR+torch", "name:garlic", "^garlic", "-garlic",
    ")(", "garlic+AND", "%22garlic%22+%22", "garlic%00cream", "{name}:x", "🧄",
])
def test_search_syntax_is_just_text(pantry, hostile):
    response = pantry.get("/search?q=" + hostile)
    assert response.status_code in (200, 302)
    if response.status_code == 200:
        assert len(card_titles(response.get_data(as_text=True))) < 4                 # never "match everything"


def test_typed_text_is_escaped(pantry):
    html = page(pantry, "/search?q=%3Cscript%3Ealert(1)%3C/script%3E+garlic")
    assert "<script>alert" not in html
    assert 'value="&lt;script&gt;alert(1)&lt;/script&gt; garlic"' in html


def test_match_expressions_are_only_quoted_words():
    text = forms.parse_text_search(['"tom" NEAR(x) OR name:y* -z ^w'])
    assert text.match_expression() == '"tom"* "near"* "name"*'         # one-letter words are dropped


# --- canonical URLs and limits ------------------------------------------------------------------

@pytest.mark.parametrize("path, canonical", [
    ("/search?q=%20%20Garlic%20%20%20bread%20", "/search?q=Garlic+bread"),   # trimmed, spaces collapsed, case kept
    ("/search?q=", "/search"),
    ("/search?q=%21%21%21", "/search"),                                     # no words: no word search
    ("/search?q=the", "/search"),
    ("/search?q=garlic&q=cream", "/search?q=garlic"),                       # the first value decides
    ("/search?sort=cal-asc&tag=soup&q=garlic%20", "/search?q=garlic&tag=soup&sort=cal-asc"),
    ("/search?q=garlic&sort=new", "/search?q=garlic"),
    ("/search?q=cre%CC%81me", "/search?q=cr%C3%A8me".replace("%C3%A8", "%C3%A9")),   # NFC
])
def test_non_canonical_word_searches_redirect_once(pantry, path, canonical):
    assert redirects_to(pantry, path) == canonical
    assert pantry.get(canonical).status_code == 200


def test_too_long_or_too_many_words_search_nothing_and_keep_the_text(pantry):
    long = page(pantry, "/search?q=" + "garlic+" * 40)
    assert "That search is too long" in long and card_titles(long) == []
    assert re.search(r'name="q" value="(garlic ){28}garl"', long)              # the first 200 characters, to fix
    many = page(pantry, "/search?q=" + "+".join(f"word{i}" for i in range(11)))
    assert "Use up to 10 words" in many and card_titles(many) == []
    assert "word10" in many


def test_a_bad_tag_wins_but_the_words_stay_in_the_box(pantry):
    html = page(pantry, "/search?q=garlic&tag=so%20up")
    assert "That isn't a tag" in html.replace("&#39;", "'") and card_titles(html) == []
    assert 'name="q" value="garlic"' in html


# --- the page ----------------------------------------------------------------------------------

def test_the_box_comes_first_in_the_search_form(pantry):
    html = page(pantry, "/search")
    form = html[html.index('class="search-form"'):html.index("</form>", html.index('class="search-form"'))]
    assert form.index('name="q"') < form.index('name="tag"') < form.index('name="sort"')
    box = re.search(r'<input class="words-input"[^>]*>', form, re.S).group(0)
    for attr in ['type="search"', 'maxlength="200"', 'enterkeyhint="search"', 'autocapitalize="none"',
                 'autocorrect="off"', 'spellcheck="false"', 'aria-describedby="q-hint"']:
        assert attr in box
    assert "autofocus" not in box
    assert '<label class="search-legend" for="q">' in form


def test_summary_and_title_say_what_was_searched(pantry):
    html = page(pantry, "/search?q=Garlic&tag=soup")
    assert re.search(r"1 recipe mentioning .*garlic.* with .*soup.*, freshest first\.", html)
    assert "<title>Recipes mentioning garlic, tagged soup · Forkful</title>" in html
    none = page(pantry, "/search?q=anchovies")
    assert "No recipes mention" in none and "Try fewer words" in none


def test_the_tray_removes_words_or_tags_and_keeps_the_rest(pantry):
    html = page(pantry, "/search?q=garlic&tag=soup&sort=cal-desc")
    tray = html[html.index('class="search-tray"'):html.index("</ul>", html.index('class="search-tray"'))]
    assert 'class="tag-remove words-remove" href="/search?tag=soup&amp;sort=cal-desc"' in tray
    assert 'class="tag-remove" href="/search?q=garlic&amp;sort=cal-desc"' in tray
    assert 'class="clear-filter" href="/search?sort=cal-desc">Clear search' in tray
    assert 'class="tag-chip tag-chip-all" href="/search?q=garlic&amp;sort=cal-desc"' in html


def test_search_input_gets_the_phone_safe_font_size():
    from pathlib import Path
    css = (Path(__file__).parent.parent / "forkful/static/css/forkful.css").read_text()
    assert re.search(r'input\[type="search"\][^{]*{[^}]*font-size: 1\.05rem', css)


def pantry_db_path(db):
    return db.execute("PRAGMA database_list").fetchone()["file"]
