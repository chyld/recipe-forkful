"""The ⬇️ Download button: a recipe as one self-contained HTML file (PLAN.md §29)."""

import base64
import hashlib
import io
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
from PIL import Image

import factories
from conftest import recipe_data, session_csrf
from forkful import __version__, export, photos
from test_templates import _phone_css

STATIC = Path(__file__).resolve().parent.parent / "forkful" / "static"
DATA_URI = re.compile(r"data:[a-z/0-9+.-]+;base64,[A-Za-z0-9+/=]+")
URL_ATTRIBUTES = {"src", "href", "srcset", "action", "formaction", "poster", "data", "background", "xlink:href",
                  "ping", "cite", "manifest", "longdesc", "lowsrc", "icon", "codebase", "archive", "imagesrcset"}
TOO_BIG = "too much for one download"
UNREADABLE = "couldn&#39;t be read"


class Page(HTMLParser):
    """Every start tag with its attributes, the style element's text, and the page's text."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags, self.styles, self.text = [], [], []
        self._in_style = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "style":
            self._in_style = True
            self.styles.append("")

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.styles[-1] += data
        else:
            self.text.append(data)

    def all(self, name):
        return [attrs for tag, attrs in self.tags if tag == name]

    def words(self):
        """The visible text (never the inlined CSS), whitespace collapsed."""
        return " ".join(" ".join(self.text).split())


def download(client, public_id):
    response = client.get(f"/recipes/{public_id}/download")
    assert response.status_code == 200, response.status_code
    return response


def html_of(client, public_id):
    return download(client, public_id).get_data(as_text=True)


def embedded_images(html):
    """[(content type, decoded Pillow image, attributes)] for every <img>, in page order."""
    found = []
    for attrs in Page(html).all("img"):
        match = re.fullmatch(r"data:(image/(?:jpeg|png));base64,([A-Za-z0-9+/=]+)", attrs["src"])
        assert match, attrs["src"][:60]
        found.append((match.group(1), Image.open(io.BytesIO(base64.b64decode(match.group(2)))), attrs))
    return found


def meta_csp(page):
    [meta] = [m for m in page.all("meta") if m.get("http-equiv", "").lower() == "content-security-policy"]
    return meta["content"]


def photo_paths(app, alice, public_id):
    """The stored files of a recipe's photos, in the recipe's order."""
    keys = re.findall(r'<img src="/photos/([0-9a-f]{32})"', alice.get(f"/recipes/{public_id}").get_data(as_text=True))
    return [next(app.config["PHOTOS_DIR"].glob(f"{key}.*")) for key in keys]


def noisy_png(size=(240, 180), seed=0):
    """A photo with real detail, so JPEG quality and file size actually matter."""
    im = Image.effect_noise(size, 60 + seed).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def note_after(alice, public_id, response):
    """A failed download: back to the recipe, no file, and the note shown there."""
    assert response.status_code == 302 and response.headers["Location"] == f"/recipes/{public_id}"
    assert "Content-Disposition" not in response.headers
    return alice.get(f"/recipes/{public_id}").get_data(as_text=True)


# --- the response ------------------------------------------------------------------

def test_download_is_an_attachment_with_the_files_own_policy(alice):
    response = download(alice, alice.create_recipe("Margherita Pizza"))
    assert response.content_type == "text/html; charset=utf-8"
    assert response.headers["Content-Disposition"] == "attachment; filename=margherita-pizza.html"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    # Shown instead of saved, the app's own CSP would blank the styles and photos: same policy as the file.
    page = Page(response.get_data(as_text=True))
    assert response.headers["Content-Security-Policy"] == meta_csp(page) + "; frame-ancestors 'none'"


def test_only_the_owner_can_download(app, alice, bob):
    public_id = alice.create_recipe()
    assert bob.get(f"/recipes/{public_id}/download").status_code == 404
    anonymous = app.test_client().get(f"/recipes/{public_id}/download")
    assert anonymous.status_code == 302 and anonymous.headers["Location"].endswith("/login")


def test_unicode_names_get_both_filenames(alice):
    response = download(alice, alice.create_recipe("Crème brûlée"))
    assert response.headers["Content-Disposition"] == (
        "attachment; filename=creme-brulee.html; filename*=UTF-8''cr%C3%A8me-br%C3%BBl%C3%A9e.html")


@pytest.mark.parametrize("name, fallback, full", [
    ("Margherita Pizza", "margherita-pizza.html", "margherita-pizza.html"),
    ("Crème brûlée", "creme-brulee.html", "crème-brûlée.html"),
    ("Cre\u0300me", "creme.html", "crème.html"),                       # decomposed input is composed first
    ("寿司 🍣", "recipe.html", "寿司.html"),
    ("हिन्दी रेसिपी", "recipe.html", "हिन्दी-रेसिपी.html"),              # vowel signs stay with their letters
    ("İstanbul", "istanbul.html", "i\u0307stanbul.html"),
    ("🍕🍕", "recipe.html", "recipe.html"),
    ("../../etc/passwd", "etc-passwd.html", "etc-passwd.html"),
    ("Mom's \"best\" chili; x=1", "mom-s-best-chili-x-1.html", "mom-s-best-chili-x-1.html"),
    ("C:\\Temp\\a<b>|c?*", "c-temp-a-b-c.html", "c-temp-a-b-c.html"),
    ("tea_time\r\nnow", "tea-time-now.html", "tea-time-now.html"),
    ("CON", "recipe-con.html", "recipe-con.html"),
    ("lpt1", "recipe-lpt1.html", "recipe-lpt1.html"),
    ("COM¹", "recipe-com1.html", "recipe-com¹.html"),                    # Windows reserves these too
    ("lpt³!", "recipe-lpt3.html", "recipe-lpt³.html"),
    ("a" * 59 + " b", "a" * 59 + ".html", "a" * 59 + ".html"),         # cut at 60, no dangling dash
    ("x" * 120, "x" * 60 + ".html", "x" * 60 + ".html"),
])
def test_filenames(name, fallback, full):
    assert export.filenames(name) == (fallback, full)
    options = export.disposition(name)
    assert options["filename"] == fallback
    assert ("filename*" in options) == (full != fallback)
    assert not re.search(r"[\x00-\x1f\"\\/]", "".join(options.values()))


def test_a_head_request_leaves_no_note_behind(app, alice):
    public_id = alice.create_recipe(photos=[("a.png", noisy_png())])
    app.config["MAX_EXPORT_BYTES"] = 2000
    response = alice.open(f"/recipes/{public_id}/download", method="HEAD")
    assert response.status_code == 302 and "Content-Disposition" not in response.headers
    assert TOO_BIG not in alice.get(f"/recipes/{public_id}").get_data(as_text=True)


# --- self-contained ----------------------------------------------------------------

def full_recipe(alice):
    """A remix with tags, ingredients, multi-line instructions, four photos and a remix of its own."""
    parent = alice.create_recipe("Grandma's Pizza")
    child = alice.fork(parent, "Margherita Pizza").headers["Location"].rsplit("/", 1)[1]
    data = recipe_data("Margherita Pizza", ingredients=[("Pizza dough", "1 ball", 650), ("Basil", "", 2)],
                       instructions="1. Stretch the dough.\n2. Bake hot.\n\n  Eat!",
                       photos=[("a.jpg", factories.jpeg(color="red")), ("b.png", factories.png(color="lime")),
                               ("c.webp", factories.webp(color="blue")), ("d.png", factories.rgba_png())],
                       tags="italian quick")
    assert alice.post(f"/recipes/{child}/edit", data=data, content_type="multipart/form-data").status_code == 302
    alice.fork(child, "Margherita Pizza Bianca")              # a remix of it: its name must stay out
    return parent, child


def test_the_file_needs_nothing_from_outside(alice):
    _, public_id = full_recipe(alice)
    html = html_of(alice, public_id)
    page = Page(html)
    names = {tag for tag, _ in page.tags}
    assert not names & {"script", "link", "form", "iframe", "frame", "object", "embed", "base", "input", "button",
                        "a", "video", "audio", "source", "track", "svg", "math", "noscript", "template", "portal",
                        "dialog"}
    for tag, attrs in page.tags:
        for name, value in attrs.items():
            assert not name.startswith("on"), (tag, name)
            assert name != "style", tag
            if name in URL_ATTRIBUTES:
                assert value.startswith(("data:", "#")), (tag, name, value[:60])
    assert [m.get("http-equiv") for m in page.all("meta") if "http-equiv" in m] == ["Content-Security-Policy"]
    [css] = page.styles
    assert re.findall(r"url\(", css) and all(u.startswith("data:") for u in export.URL_RE.findall(css))
    assert "@import" not in css.lower()
    outside = DATA_URI.sub("", html)
    for text in ["http:", "https:", "//localhost", "/static/", "/photos/", "/recipes/", "/search", "src=\"/"]:
        assert text not in outside, text


def test_the_style_is_allowed_by_its_hash_and_nothing_else_is(alice):
    page = Page(html_of(alice, alice.create_recipe()))
    [css] = page.styles
    digest = base64.b64encode(hashlib.sha256(css.encode("utf-8")).digest()).decode()
    assert meta_csp(page) == (
        f"default-src 'none'; img-src data:; font-src data:; style-src 'sha256-{digest}'; script-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'")
    # The charset first, then the CSP, before anything that could load or style.
    assert [tag for tag, _ in page.tags][:4] == ["html", "head", "meta", "meta"]
    assert page.all("meta")[0] == {"charset": "utf-8"}
    assert page.all("meta")[1]["http-equiv"] == "Content-Security-Policy"


def test_the_whole_stylesheet_and_the_font_go_in(alice):
    [css] = Page(html_of(alice, alice.create_recipe())).styles
    font = base64.b64encode((STATIC / "fonts" / "fredoka-latin-wght.woff2").read_bytes()).decode()
    site, extra = ((STATIC / "css" / name).read_text() for name in ("forkful.css", "export.css"))
    assert css == (site + "\n" + extra).replace(export.FONT_URL, f'url("data:font/woff2;base64,{font}")')
    assert "@media print" in extra and ".photo-grid" in extra


def test_the_stylesheets_reach_for_nothing_but_the_font():
    # Every url() in the inlined CSS must become data:, so a new one would break downloads (on purpose).
    assert re.findall(r"url\([^)]*\)", (STATIC / "css" / "forkful.css").read_text()) == [export.FONT_URL]
    exported = (STATIC / "css" / "export.css").read_text()
    assert "url(" not in exported and "@import" not in exported and not re.search(r"https?:", exported)


@pytest.mark.parametrize("extra", ['.x { background: url("/static/img/a.png") }', ".x { background: url(a.png) }",
                                   '@import "x.css";', "/* </style><script>x</script> */",
                                   '.x { background: u\\72l("https://example.com/a.png") }'])   # an escaped url(
def test_the_stylesheet_fails_closed(app, tmp_path, extra):
    static = tmp_path / "static"
    shutil.copytree(STATIC, static)
    with open(static / "css" / "export.css", "a") as f:
        f.write("\n" + extra + "\n")
    app.static_folder = str(static)
    with app.app_context(), pytest.raises(RuntimeError):
        export.stylesheet()


def test_a_changed_font_url_fails_closed(app, tmp_path):
    static = tmp_path / "static"
    shutil.copytree(STATIC, static)
    css = static / "css" / "forkful.css"
    css.write_text(css.read_text().replace("fredoka-latin-wght.woff2", "fredoka.woff2"))
    app.static_folder = str(static)
    with app.app_context(), pytest.raises(RuntimeError):
        export.stylesheet()


def test_a_stylesheet_that_fails_closed_is_an_error_not_a_file(alice, monkeypatch):
    public_id = alice.create_recipe()

    def refuse():
        raise RuntimeError("export: test")
    monkeypatch.setattr(export, "stylesheet", refuse)
    response = alice.get(f"/recipes/{public_id}/download")
    assert response.status_code == 500 and "Content-Disposition" not in response.headers


# --- what's in it --------------------------------------------------------------------

def test_everything_in_the_recipe_is_there(alice):
    _, public_id = full_recipe(alice)
    html = html_of(alice, public_id)
    words = Page(html).words()
    assert "<title>Margherita Pizza</title>" in html
    for expected in ["Remix of Grandma's Pizza", "Margherita Pizza", "italian", "quick", "Pizza dough", "1 ball",
                     "650 cal", "Basil", "2 cal", "Total: 652 calories", "Ingredients", "Instructions", "Photos"]:
        assert expected in words, expected
    assert '<span class="stamp-number">652</span>' in html                # the calorie stamp
    assert '<div class="instructions">1. Stretch the dough.\n2. Bake hot.\n\n  Eat!</div>' in html
    assert re.findall(r'<span class="tag-name">(\w+)</span>', html) == ["italian", "quick"]

    images = embedded_images(html)
    assert [a["alt"] for _, _, a in images] == [f"Photo {n} of Margherita Pizza" for n in range(1, 5)]
    assert html.index('<div class="hero-photos">') < html.index(images[0][2]["src"]) < html.index('<ul class="photo-grid"')
    colors = [im.convert("RGBA").getpixel((5, 5)) for _, im, _ in images]
    assert colors[0][0] > 200 and colors[1][1] > 200 and colors[2][2] > 200 and colors[3][3] < 255   # in order


def test_the_chosen_cover_leads(alice):
    public_id = alice.create_recipe("Soup", photos=[("a.jpg", factories.jpeg(color="red")),
                                                    ("b.jpg", factories.jpeg(color="blue"))])
    blue = re.findall(r'value="photo-([0-9a-f]{32})"', alice.get(f"/recipes/{public_id}/edit").get_data(as_text=True))[1]
    assert alice.edit_recipe(public_id, "Soup", cover=f"photo-{blue}").status_code == 302
    [(_, cover, _), (_, other, _)] = embedded_images(html_of(alice, public_id))
    assert cover.getpixel((5, 5))[2] > 200 and other.getpixel((5, 5))[0] > 200


def test_what_stays_out(alice, clock):
    _, public_id = full_recipe(alice)
    clock.advance(days=3)                                   # the recipe is from Oct 29; the download is Nov 1
    html = html_of(alice, public_id)
    outside = re.sub(r"<style>.*?</style>", "", DATA_URI.sub("", html), flags=re.S)   # the CSS's comments aside
    assert "Saved from Forkful on Nov 1, 2026" in Page(html).words()
    for absent in ["Oct 29", "2026-10-29", __version__, "Bianca", "Recipe family", "Remixes of this recipe",
                   "Fork it", "Edit recipe", "Log out", "<dialog", "Cover"]:
        assert absent not in outside, absent


def test_one_photo_goes_in_the_band_with_no_grid(alice):
    html = html_of(alice, alice.create_recipe(photos=[("a.jpg", factories.jpeg())]))
    assert len(embedded_images(html)) == 1
    assert '<div class="hero-photos">' in html and '<ul class="photo-grid"' not in html


def test_an_empty_recipe_downloads(alice):
    html = html_of(alice, alice.create_recipe("Nothing yet"))
    page = Page(html)
    assert "No ingredients listed." in page.words() and "No instructions written." in page.words()
    classes = {c for _, attrs in page.tags for c in attrs.get("class", "").split()}
    assert not page.all("img") and not classes & {"hero-photos", "photo-grid", "recipe-tags", "crumb"}


def test_photos_are_shrunk_but_never_enlarged(alice):
    html = html_of(alice, alice.create_recipe(photos=[("big.jpg", factories.jpeg(size=(3000, 2000))),
                                                      ("small.jpg", factories.jpeg(size=(64, 48))),
                                                      ("tall.png", factories.png(size=(900, 2400)))]))
    sizes = [(im.size, attrs["width"], attrs["height"]) for _, im, attrs in embedded_images(html)]
    assert sizes == [((1600, 1067), "1600", "1067"), ((64, 48), "64", "48"), ((600, 1600), "600", "1600")]


def test_photos_are_jpeg_at_quality_82(alice):
    [(mime, im, _)] = embedded_images(html_of(alice, alice.create_recipe(photos=[("n.png", noisy_png())])))
    reference = io.BytesIO()
    Image.new("RGB", (8, 8)).save(reference, "JPEG", quality=82)
    assert mime == "image/jpeg" and im.quantization == Image.open(reference).quantization


def test_transparency_is_kept_and_everything_else_is_jpeg(alice):
    opaque_rgba = io.BytesIO()
    Image.new("RGBA", (20, 20), (0, 128, 0, 255)).save(opaque_rgba, "PNG")
    html = html_of(alice, alice.create_recipe(photos=[("see-through.png", factories.rgba_png()),
                                                      ("solid-alpha.png", opaque_rgba.getvalue()),
                                                      ("photo.webp", factories.webp())]))
    images = embedded_images(html)
    assert [mime for mime, _, _ in images] == ["image/png", "image/jpeg", "image/jpeg"]
    assert images[0][1].mode == "RGBA" and images[0][1].getpixel((3, 3))[3] == 100


def test_the_copy_drops_metadata_but_keeps_color(app, alice):
    # Uploads are already clean, so put a file with EXIF, GPS, rotation and an ICC profile straight on disk.
    public_id = alice.create_recipe(photos=[("a.jpg", factories.jpeg())])
    [path] = photo_paths(app, alice, public_id)
    path.write_bytes(factories.jpeg_phone(size=(40, 20)))
    [(_, im, _)] = embedded_images(html_of(alice, public_id))
    assert not im.getexif() and im.info.get("icc_profile") == factories.srgb_icc()
    assert im.size == (20, 40)                                          # turned upright, then the tag dropped


def test_hostile_text_stays_text(alice):
    parent = alice.create_recipe('"><img src=x onerror=alert(0)>')
    child = alice.fork(parent, "</title><script>alert(1)</script>").headers["Location"].rsplit("/", 1)[1]
    data = recipe_data("</title><script>alert(1)</script>", ingredients=[("<img src=x onerror=alert(2)>", '"><b>bold</b>', 5)],
                       instructions="</style><script>alert(3)</script>\n<a href=//evil>x</a>",
                       photos=[("a.jpg", factories.jpeg()), ("b.jpg", factories.jpeg())])
    assert alice.post(f"/recipes/{child}/edit", data=data, content_type="multipart/form-data").status_code == 302
    response = download(alice, child)
    html = response.get_data(as_text=True)
    page = Page(html)
    assert {tag for tag, _ in page.tags} <= {"html", "head", "meta", "title", "style", "body", "main", "article",
                                             "header", "div", "h1", "span", "ul", "li", "section", "h2", "p",
                                             "strong", "footer", "img"}
    assert all(set(attrs) == {"src", "width", "height", "alt"} for attrs in page.all("img"))
    assert [a["alt"] for a in page.all("img")] == [f"Photo {n} of </title><script>alert(1)</script>" for n in (1, 2)]
    assert "&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Remix of &#34;&gt;&lt;img src=x onerror=alert(0)&gt;" in html
    assert "&lt;img src=x onerror=alert(2)&gt;" in html and "&#34;&gt;&lt;b&gt;bold&lt;/b&gt;" in html
    assert "&lt;/style&gt;&lt;script&gt;alert(3)&lt;/script&gt;" in html
    assert response.headers["Content-Disposition"] == "attachment; filename=title-script-alert-1-script.html"


def test_no_private_details_in_the_file(alice):
    public_id = alice.create_recipe("Soup", photos=[("a.jpg", factories.jpeg())])
    show = alice.get(f"/recipes/{public_id}").get_data(as_text=True)
    file_key = show.split("/photos/")[1][:32]
    outside = DATA_URI.sub("", html_of(alice, public_id))
    for secret in [session_csrf(alice.client), file_key, public_id, "alice", "Log out", "csrf"]:
        assert secret not in outside, secret


# --- when it can't be made ----------------------------------------------------------------

def test_the_limit_is_for_the_whole_file(app, alice):
    public_id = alice.create_recipe(photos=[("a.png", noisy_png(seed=1)), ("b.png", noisy_png(seed=2))])
    size = len(download(alice, public_id).get_data())
    app.config["MAX_EXPORT_BYTES"] = size                                 # exactly fits
    assert len(download(alice, public_id).get_data()) == size
    app.config["MAX_EXPORT_BYTES"] = size - 1                             # one byte over: styles and text count
    assert TOO_BIG in note_after(alice, public_id, alice.get(f"/recipes/{public_id}/download"))
    plain = alice.create_recipe("No photos")                              # the styles and font alone are ~80 KB
    app.config["MAX_EXPORT_BYTES"] = 2000
    assert TOO_BIG in note_after(alice, plain, alice.get(f"/recipes/{plain}/download"))


def test_too_many_photos_stop_before_the_rest_are_made(app, alice, monkeypatch):
    public_id = alice.create_recipe(photos=[("a.png", noisy_png(seed=1)), ("b.png", noisy_png(seed=2)),
                                            ("c.png", noisy_png(seed=3))])
    calls = []
    real = photos.export_copy
    monkeypatch.setattr(photos, "export_copy", lambda *args: calls.append(1) or real(*args))
    with app.app_context():
        css = export.stylesheet()
    app.config["MAX_EXPORT_BYTES"] = len(css) + 1000                      # not even one photo fits
    assert TOO_BIG in note_after(alice, public_id, alice.get(f"/recipes/{public_id}/download"))
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["delete", "garbage", "too-many-pixels"])
def test_an_unreadable_photo_stops_with_a_note_and_no_file(app, alice, damage):
    public_id = alice.create_recipe(photos=[("a.jpg", factories.jpeg()), ("b.jpg", factories.jpeg())])
    victim = photo_paths(app, alice, public_id)[1]
    if damage == "delete":
        victim.unlink()
    elif damage == "garbage":
        victim.write_bytes(b"not a photo at all")
    else:
        app.config["MAX_PHOTO_PIXELS"] = 100
    assert UNREADABLE in note_after(alice, public_id, alice.get(f"/recipes/{public_id}/download"))


def test_nothing_is_cached(app, alice):
    public_id = alice.create_recipe(photos=[("a.jpg", factories.jpeg())])
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in app.config["PHOTOS_DIR"].iterdir())
    download(alice, public_id)
    assert sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in app.config["PHOTOS_DIR"].iterdir()) == before
    photo_paths(app, alice, public_id)[0].unlink()                       # gone from disk: the next download knows
    assert UNREADABLE in note_after(alice, public_id, alice.get(f"/recipes/{public_id}/download"))


# --- the button -------------------------------------------------------------------------

def test_the_recipe_page_has_the_button(alice):
    public_id = alice.create_recipe(ingredients=[("Flour", "2 cups", 650), ("Salt", "", 2)])
    html = alice.get(f"/recipes/{public_id}").get_data(as_text=True)
    actions = html[html.index('<div class="band-actions">'):]
    actions = actions[:actions.index("</div>")]
    assert re.findall(r'<a class="btn ([\w -]+)"', actions) == ["btn-grape", "btn-lemon", "btn-plain btn-download"]
    link = re.search(r'<a class="btn btn-plain btn-download" href="([^"]+)"([^>]*)>(.*?)</a>', actions, re.S)
    assert link.group(1) == f"/recipes/{public_id}/download"
    assert " download" not in link.group(2) and 'aria-label="Download as one HTML file"' in link.group(2)
    assert '<span class="download-label">Download</span>' in link.group(3)
    assert "Total: <strong>652 calories</strong>" in html                # the page's own ingredient list


def test_the_button_rules():
    css = (STATIC / "css" / "forkful.css").read_text()
    # Wherever the buttons get less than ~500px (it's their own width that counts), Download is an icon.
    assert re.search(r"\.band-actions\s*{\s*container-type:\s*inline-size;\s*}", css)
    narrow = css[css.index("@container (max-width: 499px) {"):]
    narrow = narrow[:narrow.index("\n  }\n")]
    assert ".band-actions .btn-download { width: 48px;" in narrow and ".download-label { display: none; }" in narrow
    # Phones: its own full-width row, with its label.
    assert re.search(r"\.band-actions \.btn-download\s*{\s*flex-basis:\s*100%", _phone_css())
