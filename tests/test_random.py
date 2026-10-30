"""The header's 🎲 Random button: /random jumps to one of your recipes, picked at random."""

import re


def picked(client, path="/random"):
    response = client.get(path)
    assert response.status_code == 302, response.status_code
    match = re.fullmatch(r"/recipes/([0-9a-z]{12})", response.headers["Location"])
    assert match, response.headers["Location"]
    return match.group(1)


def test_random_lands_on_each_of_your_recipes(alice):
    mine = {alice.create_recipe(name) for name in ("Soup", "Stew", "Salad")}
    seen = {picked(alice) for _ in range(80)}          # missing one in 80 tries: about 1 in 10^14
    assert seen == mine


def test_random_never_picks_someone_elses_recipe(alice, bob):
    alices = alice.create_recipe("Alice's soup")
    bob.create_recipe("Bob's stew")
    assert {picked(alice) for _ in range(20)} == {alices}
    assert {picked(alice, f"/random?skip={alices}") for _ in range(5)} == {alices}   # skip can't reach past you


def test_random_from_a_recipe_moves_to_another(alice):
    first, second = alice.create_recipe("First"), alice.create_recipe("Second")
    assert {picked(alice, f"/random?skip={first}") for _ in range(20)} == {second}
    assert {picked(alice, f"/random?skip={second}") for _ in range(20)} == {first}


def test_with_one_recipe_random_still_goes_there(alice):
    only = alice.create_recipe("Only")
    assert picked(alice, f"/random?skip={only}") == only


def test_a_bad_skip_is_ignored(alice):
    only = alice.create_recipe("Only")
    for skip in ["%3Cscript%3E", only.upper(), "8", ""]:
        assert picked(alice, f"/random?skip={skip}") == only


def test_no_recipes_goes_home_with_a_note(alice):
    response = alice.get("/random")
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert "No recipes to pick from yet" in alice.get("/").get_data(as_text=True)


def test_the_header_button_skips_the_recipe_you_are_on(alice):
    recipe = alice.create_recipe("Soup")
    button = re.compile(r'<a class="btn btn-small btn-mint topnav-random" href="([^"]+)" aria-label="Random recipe">')
    assert button.search(alice.get("/").get_data(as_text=True)).group(1) == "/random"
    assert button.search(alice.get(f"/recipes/{recipe}").get_data(as_text=True)).group(1) == f"/random?skip={recipe}"
    assert button.search(alice.get(f"/recipes/{recipe}/edit").get_data(as_text=True)).group(1) == f"/random?skip={recipe}"
    assert button.search(alice.get("/recipes/new").get_data(as_text=True)).group(1) == "/random"


def test_random_needs_a_login(app, alice):
    alice.create_recipe("Soup")
    response = app.test_client().get("/random")
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
