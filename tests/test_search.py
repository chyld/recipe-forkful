"""The search page (v1.6): tags plus a sort by calories, and the home page's old links."""

import re

import pytest

from conftest import recipe_data
from forkful import repo


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


def cooked(client, name, calories, tags=""):
    data = recipe_data(name, [(f"{name} base", "1", c) for c in calories])
    data["tags"] = tags
    response = client.post("/recipes/new", data=data, content_type="multipart/form-data")
    assert response.status_code == 302, response.get_data(as_text=True)
    return response.headers["Location"].rsplit("/", 1)[1]


@pytest.fixture
def kitchen(alice, clock):
    """Five recipes, oldest first: totals 100, 900, 300, 0 (no ingredients) and 300."""
    for name, calories, tags in [("Salad", [60, 40], "light vegan"), ("Burger", [900], "dinner"),
                                 ("Soup", [300], "dinner vegan"), ("Water", [], ""), ("Toast", [250, 50], "vegan")]:
        cooked(alice, name, calories, tags)
        clock.advance(minutes=1)
    return alice


# --- sorting ------------------------------------------------------------------------------

def test_freshest_first_by_default(kitchen):
    assert card_titles(page(kitchen, "/search")) == ["Toast", "Water", "Soup", "Burger", "Salad"]


def test_sort_by_calories_both_ways_with_ties_freshest_first(kitchen):
    assert card_titles(page(kitchen, "/search?sort=cal-asc")) == ["Water", "Salad", "Toast", "Soup", "Burger"]
    assert card_titles(page(kitchen, "/search?sort=cal-desc")) == ["Burger", "Toast", "Soup", "Salad", "Water"]


def test_sort_works_with_a_tag_search(kitchen):
    assert card_titles(page(kitchen, "/search?tag=vegan&sort=cal-asc")) == ["Salad", "Toast", "Soup"]
    assert card_titles(page(kitchen, "/search?tag=dinner&tag=vegan&sort=cal-desc")) == ["Soup"]


def test_summary_line_says_how_it_is_sorted(kitchen):
    assert "All 5 recipes, freshest first." in page(kitchen, "/search")
    assert "All 5 recipes, fewest calories first." in page(kitchen, "/search?sort=cal-asc")
    html = page(kitchen, "/search?tag=vegan&sort=cal-desc")
    assert re.search(r"3 recipes with .*vegan.*, most calories first\.", html)


def test_sort_chips_are_radios_in_the_search_form(kitchen):
    html = page(kitchen, "/search?sort=cal-desc")
    form = html[html.index('class="search-form"'):html.index("</form>", html.index('class="search-form"'))]
    assert "Sort by</legend>" in form
    assert re.findall(r'name="sort" value="([\w-]+)"', form) == ["new", "cal-asc", "cal-desc"]
    assert re.findall(r'name="sort" value="([\w-]+)" checked', form) == ["cal-desc"]
    assert re.findall(r'name="sort" value="([\w-]+)" checked', page(kitchen, "/search")) == ["new"]


def test_the_stamp_steps_forward_only_when_sorting_by_calories(kitchen):
    assert 'class="recipe-grid by-calories"' in page(kitchen, "/search?sort=cal-asc")
    assert "by-calories" not in page(kitchen, "/search")
    assert "by-calories" not in page(kitchen, "/")


def test_the_sort_survives_removing_tags(kitchen):
    html = page(kitchen, "/search?tag=dinner&tag=vegan&sort=cal-desc")
    tray = html[html.index('class="search-tray"'):html.index("</ul>", html.index('class="search-tray"'))]
    assert re.findall(r'class="tag-remove" href="([^"]+)"', tray) == [
        "/search?tag=vegan&amp;sort=cal-desc", "/search?tag=dinner&amp;sort=cal-desc"]
    assert 'class="clear-filter" href="/search?sort=cal-desc">Clear search' in tray
    assert 'class="tag-chip tag-chip-all" href="/search?sort=cal-desc"' in html
    empty = page(kitchen, "/search?tag=dinner&tag=light&sort=cal-asc")
    assert 'href="/search?sort=cal-asc">Show all recipes' in empty


def test_the_sort_whitelist_is_the_only_way_into_order_by(app, kitchen, db):
    user_id = db.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()[0]
    with pytest.raises(KeyError):
        repo.list_recipes(db, user_id, sort="total_calories; DROP TABLE recipes")
    assert [r["name"] for r in repo.list_recipes(db, user_id, sort="cal-desc")][0] == "Burger"


def test_sorting_is_private(kitchen, bob):
    cooked(bob, "Bob's feast", [5000])
    assert card_titles(page(bob, "/search?sort=cal-asc")) == ["Bob&#39;s feast"]
    assert "Bob" not in page(kitchen, "/search?sort=cal-desc")


# --- canonical URLs -------------------------------------------------------------------------

@pytest.mark.parametrize("path, canonical", [
    ("/search?sort=new", "/search"),                                  # the default stays out of URLs
    ("/search?sort=bogus", "/search"),                                # unknown: freshest first, quietly
    ("/search?sort=", "/search"),
    ("/search?sort=CAL-ASC", "/search"),
    ("/search?sort=cal-desc&sort=cal-asc", "/search?sort=cal-desc"),  # the first value decides
    ("/search?sort=cal-asc&tag=vegan&tag=dinner", "/search?tag=dinner&tag=vegan&sort=cal-asc"),
    ("/search?tag=Vegan&sort=new", "/search?tag=vegan"),
    ("/search?tag=&sort=new", "/search"),                             # an empty form
])
def test_non_canonical_searches_redirect_once(kitchen, path, canonical):
    assert redirects_to(kitchen, path) == canonical
    assert kitchen.get(canonical).status_code == 200


def test_canonical_searches_do_not_redirect(kitchen):
    for path in ["/search", "/search?sort=cal-asc", "/search?tag=dinner&tag=vegan&sort=cal-desc"]:
        page(kitchen, path)


def test_a_bad_sort_is_never_echoed(kitchen):
    location = redirects_to(kitchen, "/search?sort=cal-asc%3Bdrop%3Cb%3E")
    assert location == "/search"
    html = page(kitchen, "/search?tag=so%20up&sort=cal-asc%3Bdrop%3Cb%3E")    # a bad tag too: no redirect
    assert "That isn't a tag" in html.replace("&#39;", "'")
    assert "drop" not in html and "<b>" not in html
    assert re.findall(r'name="sort" value="([\w-]+)" checked', html) == ["new"]


# --- the page itself --------------------------------------------------------------------------

def test_an_empty_recipe_box_gets_the_friendly_empty_state(alice):
    html = page(alice, "/search")
    assert "Your recipe box is empty!" in html and 'class="search-form"' not in html


def test_search_results_have_no_new_recipe_card(kitchen):
    assert "grid-new" not in page(kitchen, "/search")
    assert "grid-new" in page(kitchen, "/")


def test_header_search_button_on_every_page(kitchen):
    button = re.compile(r'<a class="btn btn-small btn-grape topnav-search" href="/search" aria-label="Search"( aria-current="page")?>'
                        r'<span aria-hidden="true">🔍</span><span class="nav-label">Search</span></a>')
    home = page(kitchen, "/")
    assert button.search(home).group(1) is None
    assert re.search(r'class="topnav-link" href="/" aria-current="page">My recipes', home)
    assert button.search(page(kitchen, "/search")).group(1) == ' aria-current="page"'
    recipe = re.search(r'href="(/recipes/[0-9a-z]{12})"', home).group(1)
    assert button.search(page(kitchen, recipe)).group(1) is None


# --- the home page and its old links -----------------------------------------------------------

def test_home_is_just_the_recipes(kitchen):
    html = page(kitchen, "/")
    assert card_titles(html) == ["Toast", "Water", "Soup", "Burger", "Salad"]
    assert 'name="tag"' not in html and 'name="sort"' not in html and "search-form" not in html
    assert "My recipe box" not in html and "freshest first" not in html and "Search &amp; sort" not in html
    assert '<h1 class="sr-only">My recipes</h1>' in html                 # still one h1 for screen readers


@pytest.mark.parametrize("path, target", [
    ("/?tag=vegan", "/search?tag=vegan"),
    ("/?tag=Vegan&tag=dinner&tag=vegan", "/search?tag=dinner&tag=vegan"),   # one hop, already canonical
    ("/?sort=cal-asc", "/search?sort=cal-asc"),
    ("/?tag=vegan&sort=bogus", "/search?tag=vegan"),
    ("/?tag=", "/search"),
    ("/?tag=so%20up", "/search?tag=so+up"),                                 # invalid: the search page explains
])
def test_old_home_page_searches_forward_to_the_search_page(kitchen, path, target):
    assert redirects_to(kitchen, path) == target


def test_recipe_page_tags_link_to_the_search_page(kitchen):
    recipe_id = cooked(kitchen, "Stew", [400], "dinner")
    assert 'href="/search?tag=dinner"' in page(kitchen, f"/recipes/{recipe_id}")
