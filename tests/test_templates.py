"""Static checks that keep the strict CSP true and the colors readable."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "forkful"
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))
CSS = (ROOT / "static" / "css" / "forkful.css").read_text()
JS = (ROOT / "static" / "js" / "forkful.js").read_text()


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_templates_have_no_inline_script_style_or_handlers(path):
    html = path.read_text()
    if path.name == "export.html":
        # The downloaded file is never served under the app's CSP: it carries its own, which allows
        # exactly one style element, printed verbatim, by its hash (tests/test_download.py).
        assert html.count("<style>{{ css }}</style>") == 1
        html = html.replace("<style>{{ css }}</style>", "")
    assert not re.search(r"\sstyle\s*=", html, re.I), "inline style= breaks the CSP"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handlers break the CSP"
    assert "<style" not in html.lower()
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.I | re.S):
        assert script.strip() == "", "inline <script> content breaks the CSP"
    assert "javascript:" not in html.lower()
    assert "|safe" not in html and "Markup(" not in html


@pytest.mark.parametrize("path", TEMPLATES + [ROOT / "static/css/forkful.css", ROOT / "static/css/export.css",
                                             ROOT / "static/js/forkful.js"],
                         ids=lambda p: p.name)
def test_no_third_party_urls(path):
    assert not re.search(r"https?://", path.read_text()), "everything must come from 'self'"


def test_js_never_writes_user_text_as_html():
    # innerHTML is only used once, for our own <template> markup.
    assert JS.count("innerHTML") == 2
    assert "template.innerHTML" in JS and "holder.innerHTML = html" in JS
    assert "setAttribute(\"style\"" not in JS and "insertAdjacentHTML" not in JS


# --- contrast ------------------------------------------------------------------------

def css_vars():
    root = re.search(r":root\s*{(.*?)}", CSS, re.S).group(1)
    return {name: value.upper() for name, value in re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})\s*;", root)}


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    la, lb = sorted([luminance(a), luminance(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


AA_TEXT = 4.5
AA_LARGE = 3.0

PAIRS = [
    ("ink", "paper", AA_TEXT), ("ink", "card", AA_TEXT),
    ("muted", "paper", AA_TEXT), ("muted", "card", AA_TEXT),
    ("error", "paper", AA_TEXT), ("error", "card", AA_TEXT),
    ("ink", "tomato", AA_TEXT), ("ink", "tangerine", AA_TEXT), ("ink", "lemon", AA_TEXT),
    ("ink", "mint", AA_TEXT), ("ink", "bubblegum", AA_TEXT),
    ("ink", "blueberry", AA_TEXT),           # 4.53:1, though the plan reserves it for large/bold text
    ("white", "grape", AA_TEXT),
]


@pytest.mark.parametrize("text, background, minimum", PAIRS)
def test_color_pairs_meet_wcag_aa(text, background, minimum):
    colors = css_vars()
    ratio = contrast(colors[text], colors[background])
    assert ratio >= minimum, f"{text} on {background} is {ratio:.2f}:1"


def test_placeholder_text_is_readable():
    placeholder = re.search(r"::placeholder\s*{\s*color:\s*(#[0-9A-Fa-f]{6})", CSS).group(1)
    assert contrast(placeholder, css_vars()["card"]) >= AA_TEXT


def test_each_flavor_uses_its_safe_text_color():
    expected = {"tomato": "ink", "tangerine": "ink", "lemon": "ink", "mint": "ink",
                "blueberry": "ink", "grape": "white", "bubblegum": "ink"}
    blocks = dict(re.findall(r"\.flavor-\d\s*{[^}]*?--flavor:\s*var\(--([a-z]+)\);\s*--flavor-ink:\s*var\(--([a-z]+)\)", CSS))
    assert blocks == expected


def test_flavor_classes_match_the_python_list():
    from forkful import FLAVORS
    found = re.findall(r"\.flavor-(\d)\s*{\s*/\*\s*([a-z]+)", CSS)
    assert [name for _, name in sorted(found)] == FLAVORS


def test_button_colors_are_readable():
    colors = css_vars()
    buttons = re.findall(r"\.btn-([a-z]+)\s*{\s*--btn-bg:\s*var\(--([a-z]+)\);(?:\s*--btn-ink:\s*var\(--([a-z]+)\);)?", CSS)
    assert {name for name, _, _ in buttons} >= {"tomato", "lemon", "mint", "grape", "plain"}
    for name, background, ink in buttons:
        ratio = contrast(colors[ink or "ink"], colors[background])
        assert ratio >= AA_TEXT, f".btn-{name}: {ratio:.2f}:1"


def test_hidden_attribute_always_wins():
    assert re.search(r"\[hidden\]\s*{\s*display:\s*none\s*!important;\s*}", CSS)


def test_long_words_wrap_everywhere():
    assert re.search(r"\*, \*::before, \*::after\s*{[^}]*overflow-wrap:\s*anywhere", CSS)


def test_stamps_and_stickers_never_break_mid_word():
    # The universal overflow-wrap rule reaches child elements directly, so these opt out explicitly.
    assert re.search(r"\.stamp, \.stamp \*, \.sticker, \.sticker \*\s*{\s*overflow-wrap:\s*normal;", CSS)


def _phone_css():
    start = CSS.index("@media (max-width: 560px) {\n  html { font-size: 16px; }")
    return CSS[start:CSS.index("\n}\n", start)]


def test_phone_header_is_one_row():
    phone = _phone_css()
    assert re.search(r"\.topbar\s*{[^}]*flex-wrap:\s*nowrap", phone)
    assert re.search(r"\.topnav-link, \.who, \.wide-only\s*{\s*display:\s*none", phone)


def test_touch_targets_are_at_least_44px():
    for selector in [".brand", ".topnav-link", ".link-button", ".thumb-toggle", ".thumb-delete", ".ing-remove", ".chip"]:
        rule = re.search(re.escape(selector) + r"\s*{([^}]*)}", CSS).group(1)
        size = re.search(r"(?:min-)?height:\s*(\d+)px", rule)
        assert size and int(size.group(1)) >= 44, selector


def test_photos_open_in_the_same_tab():
    assert 'target="_blank"' not in (ROOT / "templates/show.html").read_text()


def test_tag_bar_contains_its_screen_reader_text():
    # Without position: relative, the absolutely-positioned sr-only text inside the phone's
    # scrolling tag row escapes it and makes the whole page scroll sideways.
    assert re.search(r"\.tag-bar ul\s*{[^}]*position:\s*relative", CSS)


def test_no_animations():
    # By request (v1.7.2): nothing slides, fades, wobbles, lifts or throws confetti.
    assert "@keyframes" not in CSS
    assert re.findall(r"(?:transition|animation)\s*:\s*([^;]+);", CSS) == ["none !important", "none !important"]
    assert not re.search(r":hover[^{]*{[^}]*(?:translate|rotate|transform|scale)", CSS)
    assert "requestAnimationFrame" not in JS and "confetti" not in JS
    assert ".animate(" not in JS and "smooth" not in JS


def test_no_underline_on_hover():
    assert not re.search(r":hover[^{]*{[^}]*text-decoration\s*:\s*underline", CSS)
