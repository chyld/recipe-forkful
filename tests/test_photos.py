import io
import os
import re

import pytest
from PIL import Image

import factories
from conftest import photo_files, recipe_data
from forkful import photos, repo


def photo_rows(db):
    return [dict(r) for r in db.execute("SELECT * FROM photos ORDER BY recipe_id, position")]


def stored_image(app, row):
    path = app.config["PHOTOS_DIR"] / f'{row["file_key"]}.{photos.EXTENSIONS[row["content_type"]]}'
    with open(path, "rb") as f:
        data = f.read()
    return data, Image.open(io.BytesIO(data))


@pytest.mark.parametrize("filename, content, content_type", [
    ("a.jpg", factories.jpeg(), "image/jpeg"),
    ("b.png", factories.png(), "image/png"),
    ("c.webp", factories.webp(), "image/webp"),
    ("d.jpg", factories.mpo(), "image/jpeg"),
])
def test_accepted_formats(app, alice, db, filename, content, content_type):
    alice.create_recipe("Pic", photos=[(filename, content)])
    [row] = photo_rows(db)
    assert row["content_type"] == content_type
    data, im = stored_image(app, row)
    assert (im.width, im.height, len(data)) == (row["width"], row["height"], row["byte_size"])
    assert im.format == {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[content_type]


@pytest.mark.parametrize("filename, content, message", [
    ("notes.jpg", factories.text_as_jpg(), "Please use a JPEG, PNG, or WebP photo."),
    ("anim.gif", factories.gif(), "Please use a JPEG, PNG, or WebP photo."),
    ("phone.heic", factories.fake_heic(), "Please use a JPEG, PNG, or WebP photo."),
    ("bomb.png", factories.bomb_png(), "too many pixels"),
    ("huge.png", factories.big_pixels_png(), "too many pixels"),
    ("big.jpg", os.urandom(700 * 1024), "up to 0 MB each"),
    ("empty.jpg", b"", "that file is empty"),
])
def test_rejected_uploads_leave_nothing_behind(app, alice, db, filename, content, message):
    response = alice.post("/recipes/new", data=recipe_data("Pic", photos=[("ok.png", factories.png()), (filename, content)]),
                          content_type="multipart/form-data")
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert filename in html and message in html.replace("&#39;", "'")
    assert photo_rows(db) == [] and photo_files(app) == []
    assert db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0


def test_blank_file_input_is_ignored(app, alice, db):
    """Browsers send one empty, unnamed part when no file was chosen."""
    data = recipe_data("No pics")
    data["photos"] = [(io.BytesIO(b""), "")]
    response = alice.post("/recipes/new", data=data, content_type="multipart/form-data")
    assert response.status_code == 302
    assert photo_rows(db) == []


def test_phone_photo_is_rotated_and_stripped_but_keeps_color_profile(app, alice, db):
    alice.create_recipe("Phone", photos=[("IMG_0001.jpg", factories.jpeg_phone(size=(40, 20)))])
    [row] = photo_rows(db)
    data, im = stored_image(app, row)
    assert (im.width, im.height) == (20, 40)                 # orientation 6 applied
    exif = im.getexif()
    assert 0x0112 not in exif and 0x010F not in exif
    assert dict(exif.get_ifd(0x8825)) == {}                  # no GPS
    assert b"PhoneMaker" not in data and b"Exif" not in data
    assert im.info.get("icc_profile") == factories.srgb_icc()


@pytest.mark.parametrize("content, mode_check", [
    (factories.cmyk_jpeg(), lambda im: im.mode == "RGB"),
    (factories.rgba_png(), lambda im: im.mode == "RGBA" and im.getpixel((0, 0))[3] == 100),
    (factories.palette_png_with_transparency(), lambda im: im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0),
    (factories.png_16bit(), lambda im: im.mode == "L" and 150 <= im.getpixel((0, 0)) <= 160),
    (factories.animated_webp(), lambda im: getattr(im, "n_frames", 1) == 1),
])
def test_color_modes_and_animation_are_normalized(app, alice, db, content, mode_check):
    alice.create_recipe("Modes", photos=[("x", content)])
    [row] = photo_rows(db)
    _, im = stored_image(app, row)
    assert mode_check(im)


def test_polyglot_payload_is_gone(app, alice, db):
    alice.create_recipe("Poly", photos=[("p.jpg", factories.jpeg_with_payload())])
    data, _ = stored_image(app, photo_rows(db)[0])
    assert b"<script>" not in data


def test_big_photos_are_capped_at_the_max_edge(app, alice, db):
    app.config["PHOTO_MAX_EDGE"] = 64
    alice.create_recipe("Big", photos=[("big.png", factories.png(size=(300, 150)))])
    row = photo_rows(db)[0]
    assert (row["width"], row["height"]) == (64, 32)


def test_delete_button_deletes_one_photo_right_away(app, alice, db, clock):
    recipe_id = alice.create_recipe("Gallery", photos=[(f"{i}.png", factories.png(color=c))
                                                       for i, c in enumerate(["red", "green", "blue"])])
    before = photo_rows(db)
    updated_before = db.execute("SELECT updated_at FROM recipes").fetchone()[0]
    clock.advance(minutes=1)
    response = alice.delete_photo(recipe_id, before[1]["file_key"])
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/recipes/{recipe_id}/edit")
    assert "Photo deleted." in alice.get(response.headers["Location"]).get_data(as_text=True)
    assert [r["id"] for r in photo_rows(db)] == [before[0]["id"], before[2]["id"]]
    assert not any(f.startswith(before[1]["file_key"]) for f in photo_files(app))   # really gone from disk
    assert len(photo_files(app)) == 2
    assert db.execute("SELECT updated_at FROM recipes").fetchone()[0] > updated_before


def test_delete_from_javascript_gets_a_204(app, alice, db):
    recipe_id = alice.create_recipe("Gallery", photos=[("a.png", factories.png())])
    file_key = photo_rows(db)[0]["file_key"]
    response = alice.delete_photo(recipe_id, file_key, fetch=True)
    assert response.status_code == 204 and response.data == b""
    assert photo_rows(db) == [] and photo_files(app) == []


def test_deleting_a_missing_photo_is_404(alice, db):
    recipe_id = alice.create_recipe("Gallery", photos=[("a.png", factories.png())])
    row = photo_rows(db)[0]
    other = alice.create_recipe("Other")
    assert alice.delete_photo(other, row["file_key"]).status_code == 404       # the photo belongs to another recipe
    assert alice.delete_photo(recipe_id, "f" * 32).status_code == 404
    assert alice.delete_photo(recipe_id, row["id"]).status_code == 404         # integer photo ids are gone
    assert alice.delete_photo(recipe_id, row["file_key"].upper()).status_code == 404
    assert alice.delete_photo(recipe_id, row["file_key"]).status_code == 302
    assert alice.delete_photo(recipe_id, row["file_key"]).status_code == 404   # already gone


def test_add_photos_while_editing(app, alice, db):
    recipe_id = alice.create_recipe("Gallery", photos=[("a.png", factories.png())])
    assert alice.edit_recipe(recipe_id, name="Gallery", photos=[("new.webp", factories.webp())]).status_code == 302
    rows = photo_rows(db)
    assert [r["content_type"] for r in rows] == ["image/png", "image/webp"]     # added at the end


def test_delete_every_photo(app, alice, db):
    recipe_id = alice.create_recipe("Gallery", photos=[("a.png", factories.png()), ("b.png", factories.png())])
    for row in photo_rows(db):
        assert alice.delete_photo(recipe_id, row["file_key"]).status_code == 302
    assert photo_rows(db) == [] and photo_files(app) == []


def test_no_cap_on_photo_count(app, alice, db):
    recipe_id = alice.create_recipe("Many", photos=[(f"{i}.png", factories.png(size=(8, 8))) for i in range(15)])
    alice.edit_recipe(recipe_id, name="Many", photos=[(f"x{i}.png", factories.png(size=(8, 8))) for i in range(15)])
    assert len(photo_rows(db)) == 30
    assert [r["position"] for r in photo_rows(db)] == list(range(30))


def test_edit_page_has_delete_buttons_not_remove_checkboxes(alice, db):
    recipe_id = alice.create_recipe("Gallery", photos=[("a.png", factories.png())])
    file_key = photo_rows(db)[0]["file_key"]
    html = alice.get(f"/recipes/{recipe_id}/edit").get_data(as_text=True)
    assert f'formaction="/recipes/{recipe_id}/photos/{file_key}/delete"' in html
    assert "remove_photo" not in html and "Deleted when you save" not in html


@pytest.mark.parametrize("failing", ["insert", "commit"])
def test_failed_save_leaves_no_new_files(app, alice, db, monkeypatch, failing):
    recipe_id = alice.create_recipe("Safe", photos=[("a.png", factories.png())])
    files_before = photo_files(app)
    if failing == "insert":
        def boom(*args, **kwargs):
            raise RuntimeError("insert failed")
        monkeypatch.setattr(repo, "add_photos", boom)
    else:
        from forkful import db as dbmod
        real_transaction = dbmod.transaction

        from contextlib import contextmanager

        @contextmanager
        def failing_commit(conn):
            with real_transaction(conn):
                yield conn
                raise RuntimeError("commit failed")
        monkeypatch.setattr("forkful.recipes.transaction", failing_commit)
    response = alice.edit_recipe(recipe_id, name="Changed", photos=[("b.png", factories.png())])
    assert response.status_code == 500
    assert photo_files(app) == files_before
    assert len(photo_rows(db)) == 1
    assert db.execute("SELECT name FROM recipes").fetchone()[0] == "Safe"


def test_serving_headers_and_revalidation(app, alice, db):
    alice.create_recipe("Serve", photos=[("a.webp", factories.webp())])
    key = photo_rows(db)[0]["file_key"]
    response = alice.get(f"/photos/{key}")
    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    assert response.headers["Cache-Control"] == "private, no-cache"
    assert "Cookie" in response.headers["Vary"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    etag = response.headers["ETag"]
    again = alice.get(f"/photos/{key}", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["Cache-Control"] == "private, no-cache"


@pytest.mark.parametrize("key", ["../secret_key", "..%2Fsecret_key", "A" * 32, "0" * 31, "0" * 32])
def test_bad_or_unknown_photo_keys_are_404(alice, key):
    assert alice.get(f"/photos/{key}").status_code == 404


def test_oversized_request_gets_the_friendly_413(app, alice, db):
    big = [(f"{i}.jpg", os.urandom(500 * 1024)) for i in range(8)]      # 4 MB > the 3 MB test limit
    response = alice.post("/recipes/new", data=recipe_data("Huge", photos=big), content_type="multipart/form-data")
    assert response.status_code == 413
    assert "nothing was saved" in response.get_data(as_text=True)
    assert photo_rows(db) == [] and photo_files(app) == []


def test_file_input_accept_list_avoids_heic_transcoding(alice):
    html = alice.get("/recipes/new").get_data(as_text=True)
    assert 'accept="image/jpeg,image/png,image/webp"' in html


def test_stale_delete_from_another_tab_cannot_delete_a_newer_photo(app, alice, db):
    recipe_id = alice.create_recipe("Tabs", photos=[("a.png", factories.png())])
    old_key = photo_rows(db)[0]["file_key"]
    # Tab B deletes the photo and adds a new one; tab A still shows the old photo's Delete button.
    assert alice.delete_photo(recipe_id, old_key).status_code == 302
    assert alice.edit_recipe(recipe_id, name="Tabs", photos=[("b.png", factories.png())]).status_code == 302
    [new_row] = photo_rows(db)
    assert new_row["file_key"] != old_key                             # keys are random, never reused
    assert alice.delete_photo(recipe_id, old_key).status_code == 404
    assert [r["id"] for r in photo_rows(db)] == [new_row["id"]]


@pytest.mark.parametrize("mode", ["L", "RGB", "1"])
def test_color_key_transparency_survives(app, alice, db, mode):
    alice.create_recipe("Key", photos=[("k.png", factories.png_with_color_key(mode))])
    _, im = stored_image(app, photo_rows(db)[0])
    assert im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0


def test_photo_responses_have_no_expires_header(app, alice, db):
    alice.create_recipe("Fresh", photos=[("a.png", factories.png())])
    key = photo_rows(db)[0]["file_key"]
    response = alice.get(f"/photos/{key}")
    assert "Expires" not in response.headers
    assert "Expires" not in alice.get(f"/photos/{key}", headers={"If-None-Match": response.headers["ETag"]}).headers


def test_photo_whose_file_vanished_is_404(app, alice, db):
    alice.create_recipe("Gone", photos=[("a.png", factories.png())])
    row = photo_rows(db)[0]
    (app.config["PHOTOS_DIR"] / f'{row["file_key"]}.png').unlink()
    assert alice.get(f'/photos/{row["file_key"]}').status_code == 404
