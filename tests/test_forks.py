import factories
from conftest import internal_id


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def fork_id(response):
    assert response.status_code == 302, response.get_data(as_text=True)
    return response.headers["Location"].rsplit("/", 1)[1]


def test_fork_copies_ingredients_and_instructions_but_not_photos(alice, db):
    parent = alice.create_recipe("Pancakes", [("Flour", "2 cups", 910), ("Egg", "1", 70)],
                                 instructions="Mix.\nFry.", photos=[("p.jpg", factories.jpeg())])
    child = fork_id(alice.fork(parent, "Vegan pancakes"))
    assert child != parent                                            # a fork gets its own public id
    row = db.execute("SELECT parent_id, name, instructions FROM recipes WHERE public_id = ?", (child,)).fetchone()
    assert tuple(row) == (internal_id(db, parent), "Vegan pancakes", "Mix.\nFry.")
    ingredients = db.execute("SELECT position, name, amount, calories FROM ingredients WHERE recipe_id = ? ORDER BY position",
                             (internal_id(db, child),)).fetchall()
    assert [tuple(i) for i in ingredients] == [(0, "Flour", "2 cups", 910), (1, "Egg", "1", 70)]
    assert db.execute("SELECT COUNT(*) FROM photos WHERE recipe_id = ?", (internal_id(db, child),)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM photos WHERE recipe_id = ?", (internal_id(db, parent),)).fetchone()[0] == 1
    assert "980" in page(alice, f"/recipes/{child}")


def test_fork_and_parent_are_independent_afterwards(alice, db):
    parent = alice.create_recipe("Soup", [("Leek", "1", 50)], instructions="Boil.")
    child = fork_id(alice.fork(parent, "Soup remix"))
    alice.edit_recipe(parent, name="Soup", ingredients=[("Leek", "3", 150)], instructions="Simmer.")
    alice.edit_recipe(child, name="Soup remix", ingredients=[("Potato", "2", 200)], instructions="Blend.")
    assert "Simmer." in page(alice, f"/recipes/{parent}") and "Leek" in page(alice, f"/recipes/{parent}")
    child_html = page(alice, f"/recipes/{child}")
    assert "Blend." in child_html and "Potato" in child_html and "Leek" not in child_html


def test_fork_leaves_the_parent_untouched_and_goes_to_the_top(alice, db, clock):
    parent = alice.create_recipe("Original")
    clock.advance(minutes=1)
    alice.create_recipe("Newer")
    before = db.execute("SELECT updated_at FROM recipes WHERE public_id = ?", (parent,)).fetchone()[0]
    clock.advance(minutes=1)
    fork_id(alice.fork(parent, "Remixed"))
    assert db.execute("SELECT updated_at FROM recipes WHERE public_id = ?", (parent,)).fetchone()[0] == before
    html = page(alice, "/")
    titles = [html.index(f'card-title">{name}<') for name in ("Remixed", "Newer", "Original")]
    assert titles == sorted(titles)


def test_forks_of_forks_and_the_family_display(alice):
    grandparent = alice.create_recipe("Bread")
    parent = fork_id(alice.fork(grandparent, "Rye bread"))
    child = fork_id(alice.fork(parent, "Seeded rye"))
    sibling = fork_id(alice.fork(parent, "Dark rye"))

    parent_html = page(alice, f"/recipes/{parent}")
    assert 'aria-label="Recipe family"' in parent_html
    assert f'href="/recipes/{grandparent}"' in parent_html                      # its parent
    assert f'href="/recipes/{child}"' in parent_html and f'href="/recipes/{sibling}"' in parent_html
    assert parent_html.index("Seeded rye") < parent_html.index("Dark rye")       # forks in creation order

    child_html = page(alice, f"/recipes/{child}")
    assert "Remix of Rye bread" in child_html
    assert f'href="/recipes/{grandparent}"' not in child_html                   # one level only

    grandparent_html = page(alice, f"/recipes/{grandparent}")
    assert "An original!" not in grandparent_html and f'href="/recipes/{parent}"' in grandparent_html
    list_html = page(alice, "/")
    assert "Remix of Bread" in list_html and "Remix of Rye bread" in list_html


def test_an_original_without_forks_says_so(alice):
    recipe_id = alice.create_recipe("Lonely")
    assert "An original!" in page(alice, f"/recipes/{recipe_id}")


def test_fork_name_must_differ_from_the_parent(alice, db):
    parent = alice.create_recipe("Pancakes")
    for bad in ["Pancakes", "  PANCAKES  ", ""]:
        response = alice.fork(parent, bad)
        assert response.status_code == 400
        html = response.get_data(as_text=True)
        assert 'role="alert"' in html
    assert db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 1


def test_siblings_may_share_a_name_and_forks_may_be_renamed_back(alice):
    parent = alice.create_recipe("Pie")
    first = fork_id(alice.fork(parent, "Pie remix"))
    fork_id(alice.fork(parent, "Pie remix"))
    assert alice.edit_recipe(first, name="Pie").status_code == 302


def test_fork_form_is_prefilled_and_fits(alice):
    parent = alice.create_recipe("x" * 120)
    html = page(alice, f"/recipes/{parent}/fork")
    assert 'value="' + "x" * 114 + ' remix"' in html
    assert fork_id(alice.fork(parent, "x" * 114 + " remix"))


def test_fork_a_recipe_with_no_ingredients(alice):
    parent = alice.create_recipe("Empty")
    child = fork_id(alice.fork(parent, "Still empty"))
    assert ">0<" in page(alice, f"/recipes/{child}")
    assert alice.get(f"/recipes/{child}/edit").status_code == 200


def test_fork_celebrates_once(alice):
    parent = alice.create_recipe("Cake")
    response = alice.post(f"/recipes/{parent}/fork", data={"name": "Cake remix"}, follow_redirects=True)
    assert 'class="toast toast-celebrate"' in response.get_data(as_text=True)
