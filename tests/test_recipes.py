import re

from conftest import internal_id, recipe_data


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_create_and_show_a_recipe(alice):
    recipe_id = alice.create_recipe(
        "Pancakes",
        [("Flour", "2 cups", 910), ("Milk", "1½ cups", 180), ("Salt", "a pinch", "")],
        instructions="Whisk.\nCook.\n\nEat warm.",
    )
    html = page(alice, f"/recipes/{recipe_id}")
    assert "Pancakes" in html
    assert "1½ cups" in html and "a pinch" in html
    assert "1,090" in html                                 # 910 + 180 + 0, with thousands separators
    assert "Whisk.\nCook.\n\nEat warm." in html             # one block, line breaks kept


def test_create_redirects_with_a_one_shot_celebration(alice):
    response = alice.post("/recipes/new", data=recipe_data("Toast"), content_type="multipart/form-data",
                          follow_redirects=True)
    html = response.get_data(as_text=True)
    assert 'class="toast toast-celebrate"' in html and "Recipe created" in html
    again = alice.get(response.request.path).get_data(as_text=True)
    assert "toast-celebrate" not in again                    # refresh doesn't repeat the message


def test_new_form_starts_with_five_blank_rows(alice):
    html = page(alice, "/recipes/new")
    assert len(re.findall(r'name="ing-\d+-name"', html)) == 5


def test_edit_overwrites_in_place_with_no_versions(alice, db):
    recipe_id = alice.create_recipe("Soup", [("Water", "1 l", 0), ("Leek", "1", 50)], instructions="Boil.")
    for round_number in range(3):
        response = alice.edit_recipe(recipe_id, name=f"Soup v{round_number}",
                                     ingredients=[("Leek", "2", 100 + round_number)], instructions="Simmer.")
        assert response.status_code == 302
    assert db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 1
    rows = db.execute("SELECT name, amount, calories FROM ingredients").fetchall()
    assert [tuple(r) for r in rows] == [("Leek", "2", 102)]
    html = page(alice, f"/recipes/{recipe_id}")
    assert "Soup v2" in html and "Simmer." in html and "Boil." not in html


def test_edit_form_shows_existing_rows_plus_three_blank(alice):
    recipe_id = alice.create_recipe("Soup", [("Water", "1 l", 0), ("Leek", "1", 50)])
    html = page(alice, f"/recipes/{recipe_id}/edit")
    assert len(re.findall(r'name="ing-\d+-name"', html)) == 5
    assert 'value="Leek"' in html


def test_total_calories_everywhere(alice):
    recipe_id = alice.create_recipe("Chili", [("Beans", "1 can", 300), ("Beef", "500 g", 1250)])
    assert "1,550" in page(alice, f"/recipes/{recipe_id}")
    assert "1,550" in page(alice, "/")
    alice.edit_recipe(recipe_id, name="Chili", ingredients=[("Beans", "1 can", 300)])
    assert "1,550" not in page(alice, "/") and ">300<" in page(alice, "/")


def test_recipe_with_no_ingredients_totals_zero(alice):
    recipe_id = alice.create_recipe("Air")
    assert ">0<" in page(alice, f"/recipes/{recipe_id}")


def test_list_is_freshest_first_and_editing_moves_to_top(alice, clock):
    first = alice.create_recipe("First")
    clock.advance(minutes=1)
    alice.create_recipe("Second")
    html = page(alice, "/")
    assert html.index("Second") < html.index("First")
    clock.advance(minutes=1)
    alice.edit_recipe(first, name="First")
    html = page(alice, "/")
    assert html.index("First") < html.index("Second")


def test_list_empty_state(alice):
    assert "Your recipe box is empty!" in page(alice, "/")


def test_validation_errors_re_render_with_input_kept(alice):
    data = recipe_data("", [("Flour", "2 cups", "2.5"), ("", "", "40")], instructions="Keep me")
    response = alice.post("/recipes/new", data=data, content_type="multipart/form-data")
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'role="alert"' in html
    assert "Give your recipe a name." in html
    assert "Use a whole number from 0 to 1,000,000, like 120." in html
    assert "Give this ingredient a name." in html
    assert 'value="2.5"' in html and 'value="Flour"' in html and "Keep me" in html


def test_html_in_user_text_is_escaped(alice):
    recipe_id = alice.create_recipe("<b>Bold</b>", [("<img src=x onerror=alert(1)>", "<i>", 1)],
                                    instructions="<script>alert(1)</script>\nline two")
    html = page(alice, f"/recipes/{recipe_id}")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;\nline two" in html
    assert "<img src=x" not in html and "<b>Bold</b>" not in html
    assert "<b>Bold</b>" not in page(alice, "/")


def test_unknown_recipe_is_404(alice):
    assert alice.get("/recipes/999999").status_code == 404
    assert alice.get("/recipes/999999/edit").status_code == 404


def test_last_save_wins(alice):
    recipe_id = alice.create_recipe("Tabs")
    alice.edit_recipe(recipe_id, name="From tab one")
    alice.edit_recipe(recipe_id, name="From tab two")
    assert "From tab two" in page(alice, f"/recipes/{recipe_id}")


def test_busy_database_gets_a_friendly_503(alice, app, monkeypatch):
    import sqlite3
    from forkful import repo

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "insert_recipe", locked)
    response = alice.post("/recipes/new", data=recipe_data("Busy"), content_type="multipart/form-data")
    assert response.status_code == 503
    assert "Someone" in response.get_data(as_text=True)


def test_error_re_render_keeps_room_for_more_ingredients(alice):
    response = alice.post("/recipes/new", data=recipe_data("", [("Flour", "1 cup", 100)]),
                          content_type="multipart/form-data")
    assert response.status_code == 400
    assert len(re.findall(r'name="ing-\d+-name"', response.get_data(as_text=True))) == 1 + 5
    recipe_id = alice.create_recipe("Soup", [("Leek", "1", 50)])
    response = alice.edit_recipe(recipe_id, name="", ingredients=[("Leek", "1", 50)])
    assert len(re.findall(r'name="ing-\d+-name"', response.get_data(as_text=True))) == 1 + 3


def test_nul_characters_never_cause_a_500(alice):
    response = alice.post("/recipes/new", data=recipe_data("\x00", [("\x00", "", 5)]), content_type="multipart/form-data")
    assert response.status_code == 400
    recipe_id = alice.create_recipe("Pan\x00cakes", [("Fl\x00our", "", 5)])
    assert "Pancakes" in page(alice, f"/recipes/{recipe_id}")
    assert alice.fork(recipe_id, "\x00").status_code == 400
    assert alice.fork(recipe_id, "Pancakes\x00").status_code == 400


def test_list_cards_show_cover_photo_or_default_image_and_flavor(alice, db):
    import factories
    from forkful import FLAVORS
    with_photo = alice.create_recipe("Photographed", photos=[("a.png", factories.png())])
    without = alice.create_recipe("Plain")
    cover_key = db.execute("SELECT file_key FROM photos").fetchone()[0]
    html = page(alice, "/")
    card_with = html[html.index(f'href="/recipes/{with_photo}"') - 200:html.index("Photographed")]
    card_without = html[html.index(f'href="/recipes/{without}"') - 200:html.index(">Plain<")]
    assert f'src="/photos/{cover_key}"' in card_with
    assert 'src="/static/img/default-recipe.svg?v=' in card_without
    for public_id, card in [(with_photo, card_with), (without, card_without)]:
        recipe_id = internal_id(db, public_id)                       # colors still come from the internal id
        assert f"flavor-{recipe_id % len(FLAVORS)}" in card and f"tilt-{recipe_id % 5}" in card
