import pytest
from werkzeug.datastructures import MultiDict

from forkful import forms


@pytest.mark.parametrize("text, expected", [
    ("", 0), ("  ", 0), ("0", 0), ("120", 120), (" 12 ", 12), ("\t7\n", 7), ("1000000", 1_000_000),
])
def test_calories_accepts_plain_whole_numbers(text, expected):
    assert forms.parse_calories(text) == (expected, None)


@pytest.mark.parametrize("text", [
    "1000001", "-0", "-5", "+5", "1_000", "٣", "１２", "1e3", "0x10", "1,000", "2.5", "007", "abc", "12 kcal",
    "9999999", "\ufeff120", "\x85120",
])
def test_calories_rejects_everything_else(text):
    assert forms.parse_calories(text) == (None, forms.CALORIES_ERROR)


def test_rows_are_ordered_by_index_and_blank_rows_dropped():
    data = MultiDict([
        ("name", "Soup"),
        ("ing-10-name", "Third"), ("ing-10-calories", "3"),
        ("ing-2-name", "First"), ("ing-2-amount", "1 cup"),
        ("ing-5-name", ""), ("ing-5-amount", ""), ("ing-5-calories", ""),
        ("ing-7-name", "Second"),
    ])
    parsed = forms.parse_recipe_form(data)
    assert not parsed.has_errors
    assert parsed.ingredients() == [(0, "First", "1 cup", 0), (1, "Second", "", 0), (2, "Third", "", 3)]


def test_partly_blank_row_is_an_error():
    parsed = forms.parse_recipe_form(MultiDict([("name", "Soup"), ("ing-0-calories", "50")]))
    assert parsed.has_errors
    assert parsed.rows[0].errors["name"] == "Give this ingredient a name."


def test_missing_field_cannot_shift_rows():
    data = MultiDict([("name", "Soup"), ("ing-0-name", "A"), ("ing-1-name", "B"), ("ing-1-calories", "9")])
    parsed = forms.parse_recipe_form(data)
    assert parsed.ingredients() == [(0, "A", "", 0), (1, "B", "", 9)]


def test_limits_match_the_schema():
    ok = forms.parse_recipe_form(MultiDict([
        ("name", "n" * 120), ("instructions", "i" * 50_000),
        ("ing-0-name", "x" * 200), ("ing-0-amount", "a" * 100), ("ing-0-calories", "1000000"),
    ]))
    assert not ok.has_errors
    too_long = forms.parse_recipe_form(MultiDict([
        ("name", "n" * 121), ("instructions", "i" * 50_001),
        ("ing-0-name", "x" * 201), ("ing-0-amount", "a" * 101), ("ing-0-calories", "1000001"),
    ]))
    assert set(too_long.errors) == {"name", "instructions"}
    assert set(too_long.rows[0].errors) == {"name", "amount", "calories"}


def test_name_is_required_but_ingredients_and_instructions_are_optional():
    assert forms.parse_recipe_form(MultiDict([("name", "   ")])).errors["name"]
    assert not forms.parse_recipe_form(MultiDict([("name", "Toast")])).has_errors


def test_too_many_ingredients():
    data = MultiDict([("name", "Big")] + [(f"ing-{i}-name", f"thing {i}") for i in range(201)])
    assert "ingredients" in forms.parse_recipe_form(data).errors


def test_text_is_normalized():
    parsed = forms.parse_recipe_form(MultiDict([
        ("name", "  Café au lait  "), ("instructions", "Line 1\r\nLine 2\r\n\r\n"),
        ("ing-0-name", " 1½ cups "), ("ing-0-amount", " a pinch "),
    ]))
    assert parsed.name == "Café au lait"          # NFC: e + combining accent becomes é
    assert parsed.instructions == "Line 1\nLine 2"
    assert parsed.rows[0].name == "1½ cups" and parsed.rows[0].amount == "a pinch"


def test_instructions_keep_their_inner_whitespace():
    parsed = forms.parse_recipe_form(MultiDict([("name", "x"), ("instructions", "\n\n    code block\n\tmore\n\n\n  ")]))
    assert parsed.instructions == "    code block\n\tmore"


def test_control_characters_are_dropped():
    parsed = forms.parse_recipe_form(MultiDict([
        ("name", "\x00Pan\x07cakes\x00"), ("instructions", "a\x00b\x1b\nc\td"),
        ("ing-0-name", "\x00"), ("ing-0-calories", "5"),
    ]))
    assert parsed.name == "Pancakes"
    assert parsed.instructions == "ab\nc\td"
    assert parsed.rows[0].errors["name"] == "Give this ingredient a name."   # NUL-only name is empty
    assert forms.parse_fork_name("Pancakes\x00", "Pancakes")[1] is not None


def test_cover_choice_is_limited_to_this_recipes_photos():
    def cover(value):
        return forms.parse_recipe_form(MultiDict([("name", "x"), ("cover", value)]), photo_keys=keys).cover
    keys = ["11111111111111111111111111111111", "22222222222222222222222222222222", "33333333333333333333333333333333"]                                   # existing photos go by file key
    assert cover("photo-" + keys[1]) == ("photo", keys[1])
    assert cover("new-0") == ("new", 0)
    for bogus in ["photo-" + "9" * 32, "photo-abc", "photo-٣", "photo-" + "9" * 4301, "new--1", "", "cover",
                  "photo-" + keys[0] + " ", "photo-" + keys[0].upper().replace("1", "A"), "photo-2"]:
        assert cover(bogus) is None, bogus


@pytest.mark.parametrize("raw, error", [
    ("Pancakes remix", None),
    ("", "Give your remix a name."),
    ("   ", "Give your remix a name."),
    ("Pancakes", "Must be different from the original name."),
    ("  PANCAKES ", "Must be different from the original name."),
    ("pancakes", "Must be different from the original name."),
    ("x" * 121, "Recipe names can be up to 120 characters."),
])
def test_fork_name_rules(raw, error):
    assert forms.parse_fork_name(raw, "Pancakes")[1] == error


def test_fork_name_compares_with_unicode_casefolding():
    assert forms.parse_fork_name("STRASSE", "Straße")[1] is not None
    assert forms.parse_fork_name("ärger", "Ärger")[1] is not None


def test_fork_suggestion_always_fits_and_differs_from_the_parent():
    assert len(forms.fork_name_suggestion("x" * 120)) == 120
    assert forms.fork_name_suggestion("Soup") == "Soup remix"
    tricky = "x" * 114 + " remix"
    suggestion = forms.fork_name_suggestion(tricky)
    assert len(suggestion) <= 120 and forms.parse_fork_name(suggestion, tricky)[1] is None


def test_calorie_pattern_is_identical_in_python_js_and_html():
    from pathlib import Path
    root = Path(forms.__file__).parent
    js = (root / "static/js/forkful.js").read_text()
    assert f"var CALORIES_RE = /^(?:{forms.CALORIES_PATTERN})$/;" in js
    assert 'pattern="{{ calories_pattern }}"' in (root / "templates/form.html").read_text()
