"""The cover photo: the first upload by default, changeable, with a default image when there are none."""

import re

import factories
from conftest import recipe_data


def cover_key(client):
    """The photo key on the first (only) card of the list page, or None for the default image."""
    html = client.get("/").get_data(as_text=True)
    match = re.search(r'src="/photos/([0-9a-f]{32})"', html)
    return match.group(1) if match else None


def keys_in_order(db, public_id):
    """A recipe's photo file keys, cover first."""
    return [r[0] for r in db.execute(
        "SELECT ph.file_key FROM photos ph JOIN recipes r ON r.id = ph.recipe_id "
        "WHERE r.public_id = ? ORDER BY ph.position, ph.id", (public_id,))]


def three_photos():
    return [(f"{c}.png", factories.png(color=c)) for c in ("red", "green", "blue")]


def test_first_upload_is_the_cover_by_default(alice, db):
    recipe_id = alice.create_recipe("Trio", photos=three_photos())
    keys = keys_in_order(db, recipe_id)
    assert cover_key(alice) == keys[0]
    html = alice.get(f"/recipes/{recipe_id}").get_data(as_text=True)
    assert html.index(keys[0]) < html.index(keys[1]) < html.index(keys[2])
    assert html.count("cover-badge") == 1


def test_choose_a_new_upload_as_cover_when_creating(alice, db):
    data = recipe_data("Trio", photos=three_photos())
    data["cover"] = "new-2"
    response = alice.post("/recipes/new", data=data, content_type="multipart/form-data")
    recipe_id = response.headers["Location"].rsplit("/", 1)[1]
    rows = db.execute("SELECT ph.file_key FROM photos ph JOIN recipes r ON r.id = ph.recipe_id "
                      "WHERE r.public_id = ? ORDER BY ph.id", (recipe_id,)).fetchall()
    assert cover_key(alice) == rows[2][0]                      # the third file uploaded
    assert keys_in_order(db, recipe_id)[0] == rows[2][0]       # and it leads the recipe page


def test_choose_an_existing_photo_as_cover_when_editing(alice, db):
    recipe_id = alice.create_recipe("Trio", photos=three_photos())
    keys = keys_in_order(db, recipe_id)
    response = alice.edit_recipe(recipe_id, name="Trio")
    assert response.status_code == 302
    data = recipe_data("Trio")
    data["cover"] = f"photo-{keys[1]}"                          # existing photos are picked by file key
    alice.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data")
    assert keys_in_order(db, recipe_id) == [keys[1], keys[0], keys[2]]
    html = alice.get(f"/recipes/{recipe_id}/edit").get_data(as_text=True)
    assert re.search(rf'value="photo-{keys[1]}"\s+checked', html)


def test_choose_a_new_upload_as_cover_when_editing(alice, db):
    recipe_id = alice.create_recipe("Duo", photos=three_photos()[:2])
    data = recipe_data("Duo", photos=[("new.webp", factories.webp())])
    data["cover"] = "new-0"
    alice.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data")
    first = db.execute("SELECT ph.content_type FROM photos ph JOIN recipes r ON r.id = ph.recipe_id "
                       "WHERE r.public_id = ? ORDER BY ph.position, ph.id LIMIT 1", (recipe_id,)).fetchone()[0]
    assert first == "image/webp"


def test_new_photos_do_not_steal_the_cover(alice, db):
    recipe_id = alice.create_recipe("Duo", photos=three_photos()[:1])
    original = keys_in_order(db, recipe_id)[0]
    alice.edit_recipe(recipe_id, name="Duo", photos=[("more.png", factories.png())])
    assert cover_key(alice) == original


def test_deleting_the_cover_promotes_the_next_photo(alice, db):
    recipe_id = alice.create_recipe("Trio", photos=three_photos())
    keys = keys_in_order(db, recipe_id)
    alice.delete_photo(recipe_id, keys[0])
    assert cover_key(alice) == keys[1]
    html = alice.get(f"/recipes/{recipe_id}/edit").get_data(as_text=True)
    assert re.search(rf'value="photo-{keys[1]}"\s+checked', html)


def test_a_cover_choice_for_a_deleted_photo_is_ignored(alice, db):
    recipe_id = alice.create_recipe("Trio", photos=three_photos())
    keys = keys_in_order(db, recipe_id)
    alice.delete_photo(recipe_id, keys[2])                      # deleted in the background...
    assert alice.edit_recipe(recipe_id, name="Trio", cover=f"photo-{keys[2]}").status_code == 302   # ...then a stale save
    assert cover_key(alice) == keys[0]


def test_bogus_cover_values_are_ignored(alice, bob, db):
    bob_recipe = bob.create_recipe("Bob's", photos=three_photos()[:1])
    bob_photo = keys_in_order(db, bob_recipe)[0]
    recipe_id = alice.create_recipe("Mine", photos=three_photos())
    before = keys_in_order(db, recipe_id)
    photo_ids = [r[0] for r in db.execute("SELECT id FROM photos")]
    for bogus in [f"photo-{bob_photo}", "new-99", "photo-abc", "new--1", "photo-" + "9" * 30, "",
                  "cover", "photo-" + before[1].upper(), f"photo-{photo_ids[-1]}"]:   # integer ids no longer work
        data = recipe_data("Mine")
        data["cover"] = bogus
        response = alice.post(f"/recipes/{recipe_id}/edit", data=data, content_type="multipart/form-data")
        assert response.status_code == 302, bogus
        assert keys_in_order(db, recipe_id) == before, bogus
    assert keys_in_order(db, bob_recipe) == [bob_photo]


def test_no_photos_means_the_default_image(alice):
    recipe_id = alice.create_recipe("Bare")
    assert cover_key(alice) is None
    assert 'src="/static/img/default-recipe.svg?v=' in alice.get("/").get_data(as_text=True)
    assert "cover-badge" not in alice.get(f"/recipes/{recipe_id}").get_data(as_text=True)


def test_a_fork_starts_with_the_default_image(alice, db):
    parent = alice.create_recipe("Original", photos=three_photos()[:1])
    response = alice.fork(parent, "Remix")
    child = response.headers["Location"].rsplit("/", 1)[1]
    html = alice.get("/").get_data(as_text=True)
    child_card = html[html.index(f'href="/recipes/{child}"'):html.index(">Remix<")]
    assert "default-recipe.svg" in child_card


def test_default_image_is_served(app):
    response = app.test_client().get("/static/img/default-recipe.svg")
    assert response.status_code == 200 and response.mimetype == "image/svg+xml"


def test_photos_are_in_the_hero_band(alice, db):
    recipe_id = alice.create_recipe("Trio", photos=three_photos())
    html = alice.get(f"/recipes/{recipe_id}").get_data(as_text=True)
    band = html[html.index('class="recipe-band"'):html.index("</header>", html.index('class="recipe-band"'))]
    keys = keys_in_order(db, recipe_id)
    assert all(f'src="/photos/{k}' in band for k in keys)
    assert band.index(keys[0]) < band.index(keys[1]) < band.index(keys[2]) < band.index('class="band-actions"')
    assert "photo-wall" not in html and "photos-section" not in html        # no second copy further down


def test_a_recipe_without_photos_shows_the_default_image_in_the_hero(alice):
    recipe_id = alice.create_recipe("Bare")
    html = alice.get(f"/recipes/{recipe_id}").get_data(as_text=True)
    band = html[html.index('class="recipe-band"'):html.index("</header>", html.index('class="recipe-band"'))]
    assert 'src="/static/img/default-recipe.svg?v=' in band and "No photos yet" in band
