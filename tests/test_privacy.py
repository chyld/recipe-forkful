"""Census tests built from app.url_map, so a newly added route can't skip auth, scoping, or CSRF."""

import pytest
from flask import url_for

import factories
from forkful.auth import PUBLIC_ENDPOINTS


def rules(app):
    return [r for r in app.url_map.iter_rules() if r.endpoint not in PUBLIC_ENDPOINTS]


def methods(rule):
    return sorted(rule.methods - {"HEAD", "OPTIONS"})


@pytest.fixture
def alice_things(app, alice):
    public_id = alice.create_recipe("Secret soup", [("Salt", "pinch", 1)],
                                    photos=[("soup.jpg", factories.jpeg())])
    html = alice.get(f"/recipes/{public_id}").get_data(as_text=True)
    file_key = html.split("/photos/")[1][:32]
    return {"app": app, "public_id": public_id, "file_key": file_key}


def build(rule, things):
    """The rule's URL for Alice's recipe and photo, built by the app's own converters."""
    with things["app"].test_request_context():
        return url_for(rule.endpoint, **{name: things[name] for name in rule.arguments})


def test_every_non_public_route_requires_login(app, alice_things):
    anonymous = app.test_client()
    checked = 0
    for rule in rules(app):
        for method in methods(rule):
            response = anonymous.open(build(rule, alice_things), method=method)
            assert response.status_code == 302, (rule.endpoint, method)
            assert response.headers["Location"].endswith("/login"), (rule.endpoint, method)
            checked += 1
    assert checked >= 10


def test_every_scoped_route_is_404_for_another_user(app, alice_things, bob):
    checked = 0
    for rule in rules(app):
        if not rule.arguments:
            continue
        for method in methods(rule):
            response = bob.open(build(rule, alice_things), method=method,
                                data={"csrf_token": bob_csrf(bob)})
            assert response.status_code == 404, (rule.endpoint, method, response.status_code)
            checked += 1
    assert checked >= 6


def bob_csrf(bob):
    with bob.client.session_transaction() as sess:
        return sess["csrf"]


def test_every_post_route_requires_csrf(app, alice_things, alice):
    checked = 0
    for rule in app.url_map.iter_rules():
        if "POST" not in rule.methods:
            continue
        path = build(rule, alice_things)
        assert alice.post(path, data={}, csrf=False).status_code == 400, rule.endpoint
        assert alice.post(path, data={"csrf_token": "forged"}, csrf=False).status_code == 400, rule.endpoint
        checked += 1
    assert checked >= 5


def test_anonymous_login_post_requires_csrf(app, users):
    client = app.test_client()
    client.get("/login")
    response = client.post("/login", data={"username": "alice", "password": "alice-password-123"})
    assert response.status_code == 400


def test_csrf_is_checked_before_ownership(app, alice_things, bob):
    path = f"/recipes/{alice_things['public_id']}/edit"
    assert bob.post(path, data={"name": "x"}, csrf=False).status_code == 400
    assert bob.post(path, data={"name": "x"}).status_code == 404


def test_list_shows_only_your_own_recipes(alice, bob):
    alice.create_recipe("Alice's lasagna")
    bob.create_recipe("Bob's burrito")
    alice_page = alice.get("/").get_data(as_text=True)
    assert "lasagna" in alice_page and "burrito" not in alice_page
    bob_page = bob.get("/").get_data(as_text=True)
    assert "burrito" in bob_page and "lasagna" not in bob_page


def test_nobody_can_delete_someone_elses_photo(app, alice, bob, db):
    alice_recipe = alice.create_recipe("A", photos=[("a.png", factories.png())])
    alice_key = db.execute("SELECT file_key FROM photos").fetchone()[0]
    bob_recipe = bob.create_recipe("B")
    assert bob.delete_photo(alice_recipe, alice_key).status_code == 404     # not his recipe
    assert bob.delete_photo(bob_recipe, alice_key).status_code == 404       # not his recipe's photo
    assert db.execute("SELECT COUNT(*) FROM photos WHERE file_key = ?", (alice_key,)).fetchone()[0] == 1


def test_bob_cannot_see_alices_photo_even_with_the_key(alice_things, bob):
    assert bob.get(f"/photos/{alice_things['file_key']}").status_code == 404


@pytest.mark.parametrize("suffix", ["", "/edit", "/fork", "/download"])
def test_old_integer_and_malformed_ids_are_404(alice, db, suffix):
    public_id = alice.create_recipe("Mine")
    internal = db.execute("SELECT id FROM recipes WHERE public_id = ?", (public_id,)).fetchone()[0]
    assert alice.get(f"/recipes/{public_id}{suffix}").status_code == 200
    for bad in [internal, 0, 2**63, public_id.upper(), public_id[:-1], public_id + "0", "_" * 12, "%20" + public_id,
                "0123456789abcdef0123456789abcdef"]:                   # v1.9's 32-hex ids are gone too
        assert alice.get(f"/recipes/{bad}{suffix}").status_code == 404, bad
    assert alice.get(f"/recipes/{public_id}{suffix}/").status_code == 404     # no trailing-slash aliases


def test_someone_elses_recipe_looks_exactly_like_no_recipe(alice, bob):
    alices = alice.create_recipe("Alice's")
    for suffix in ["", "/edit", "/fork", "/download"]:
        theirs = bob.get(f"/recipes/{alices}{suffix}")
        nothing = bob.get(f"/recipes/{'0' * 12}{suffix}")
        assert theirs.status_code == nothing.status_code == 404
        assert theirs.get_data() == nothing.get_data()
