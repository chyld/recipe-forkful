"""Recipe and fork form parsing. Pure functions over a form mapping; the limits mirror the schema."""

import re
import unicodedata
from dataclasses import dataclass, field

NAME_MAX = 120
INSTRUCTIONS_MAX = 50_000
INGREDIENT_NAME_MAX = 200
AMOUNT_MAX = 100
NOTES_MAX = 300
CALORIES_MAX = 1_000_000
MAX_INGREDIENTS = 200
NEW_BLANK_ROWS = 5
EDIT_BLANK_ROWS = 3

# Whole numbers 0..1,000,000: plain ASCII digits, no sign, separators, or leading zeros.
# The same pattern is used by the HTML `pattern` attribute and by forkful.js (a test keeps them in sync).
CALORIES_PATTERN = "0|[1-9][0-9]{0,5}|1000000"
CALORIES_RE = re.compile(CALORIES_PATTERN, re.ASCII)
# Only ASCII whitespace is trimmed, matching the JS side exactly.
ASCII_SPACE = " \t\n\r\f\v"
INGREDIENT_KEY_RE = re.compile(r"ing-([0-9]{1,4})-(name|amount|calories|notes)", re.ASCII)
MAX_ID_DIGITS = 19   # SQLite integers fit in 19 digits; longer strings aren't ids (and int() refuses > 4300)
# The cover choice: an existing photo ("photo-<file key>") or the n-th newly uploaded file ("new-<n>").
COVER_RE = re.compile(r"photo-(?P<key>[0-9a-f]{32})|new-(?P<new>[0-9]{1,19})", re.ASCII)

CALORIES_ERROR = "Use a whole number from 0 to 1,000,000, like 120."

# Tags: 2-24 letters a-z, at most 10 per recipe (mirrors migrations/0002_tags.sql).
TAG_MIN, TAG_MAX, MAX_TAGS, TAGS_FIELD_MAX = 2, 24, 10, 300
TAG_RE = re.compile(rf"[a-z]{{{TAG_MIN},{TAG_MAX}}}", re.ASCII)
TAG_SEPARATORS = re.compile(r"[,\s]+", re.ASCII)   # commas and ASCII whitespace
MAX_SEARCH_TAGS = 10      # a search can pick up to 10 tags (no recipe can have more anyway)
MAX_SEARCH_VALUES = 32    # read at most this many ?tag= values before giving up
# Word search (?q=): names, ingredient names and instructions, via the recipe_text index.
SEARCH_TEXT_MAX = 200     # characters, after normalizing
MAX_SEARCH_WORDS = 10
PREFIX_MIN = 3            # words this long also match as a prefix: "toma" finds "tomato"
# Words a search ignores, so "chicken and rice" doesn't require the recipe to say "and".
STOPWORDS = frozenset("a an and or the of to in on for with".split())
# Search sorts. "new" (freshest first) is the default: the form sends it, but URLs leave it out.
DEFAULT_SORT = "new"
CALORIE_SORTS = ("cal-asc", "cal-desc")


def _drop_controls(text, keep=""):
    """Remove control characters (NUL included: SQLite's length() stops at NUL)."""
    return "".join(ch for ch in text if ch in keep or unicodedata.category(ch) != "Cc")


def clean_line(value):
    """NFC-normalize a single-line field, drop control characters, and trim it."""
    return _drop_controls(unicodedata.normalize("NFC", value or "")).strip()


def clean_block(value):
    """NFC-normalize a multi-line field and unify newlines, keeping its inner whitespace.

    Only blank lines at the start and trailing whitespace at the end are removed, so
    an indented first line stays indented.
    """
    text = unicodedata.normalize("NFC", value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _drop_controls(text, keep="\n\t")
    return re.sub(r"\A(?:[ \t]*\n)+", "", text).rstrip()


def parse_calories(text):
    """Return (calories, error). Blank means 0."""
    text = text.strip(ASCII_SPACE)
    if text == "":
        return 0, None
    if not CALORIES_RE.fullmatch(text):
        return None, CALORIES_ERROR
    return int(text), None


@dataclass
class IngredientRow:
    name: str = ""
    amount: str = ""
    calories: str = ""
    notes: str = ""
    errors: dict = field(default_factory=dict)

    @property
    def blank(self):
        return not (self.name or self.amount or self.calories.strip() or self.notes)


@dataclass
class RecipeForm:
    name: str = ""
    instructions: str = ""
    rows: list = field(default_factory=list)          # IngredientRow, for display and saving
    cover: tuple = None                               # ("photo", file_key) or ("new", index); None keeps the default
    tags: list = field(default_factory=list)          # validated tags, in typed order
    tags_text: str = ""                               # what's shown in the Tags field
    errors: dict = field(default_factory=dict)        # "name", "tags", "instructions", "ingredients", "photos"

    @property
    def has_errors(self):
        return bool(self.errors) or any(row.errors for row in self.rows)

    def error_summary(self):
        messages = list(self.errors.values())
        for number, row in enumerate(self.rows, start=1):
            for message in row.errors.values():
                messages.append(f"Ingredient {number}: {message}")
        return messages

    def ingredients(self):
        """Validated rows as (position, name, amount, calories, notes) tuples, positions 0..n-1."""
        return [
            (position, row.name, row.amount, parse_calories(row.calories)[0], row.notes)
            for position, row in enumerate(self.rows)
        ]

    def with_blank_rows(self, count):
        self.rows = self.rows + [IngredientRow() for _ in range(count)]
        return self


def blank_form(rows=NEW_BLANK_ROWS):
    return RecipeForm().with_blank_rows(rows)


def form_from_recipe(recipe, ingredients, tags=()):
    form = RecipeForm(name=recipe["name"], instructions=recipe["instructions"],
                      tags=list(tags), tags_text=", ".join(tags))
    form.rows = [
        IngredientRow(name=i["name"], amount=i["amount"], calories=str(i["calories"]), notes=i["notes"])
        for i in ingredients
    ]
    return form.with_blank_rows(EDIT_BLANK_ROWS)


def parse_recipe_form(data, photo_keys=()):
    """Parse a submitted recipe form (a MultiDict-like mapping)."""
    form = RecipeForm(
        name=clean_line(data.get("name", "")),
        instructions=clean_block(data.get("instructions", "")),
    )

    if not form.name:
        form.errors["name"] = "Give your recipe a name."
    elif len(form.name) > NAME_MAX:
        form.errors["name"] = f"Recipe names can be up to {NAME_MAX} characters."
    if len(form.instructions) > INSTRUCTIONS_MAX:
        form.errors["instructions"] = f"Instructions can be up to {INSTRUCTIONS_MAX:,} characters."

    raw_tags = data.get("tags", "")
    form.tags_text = raw_tags[:TAGS_FIELD_MAX * 2]          # re-render what was typed (bounded)
    form.tags, tag_error = parse_tags(raw_tags)
    if tag_error:
        form.errors["tags"] = tag_error

    # Rows arrive as ing-<i>-<field>; gather by index so a missing field can't shift other rows.
    raw = {}
    for key in data.keys():
        match = INGREDIENT_KEY_RE.fullmatch(key)
        if match:
            raw.setdefault(int(match.group(1)), {})[match.group(2)] = data.get(key, "")
    for index in sorted(raw):
        values = raw[index]
        row = IngredientRow(
            name=clean_line(values.get("name", "")),
            amount=clean_line(values.get("amount", "")),
            calories=values.get("calories", "").strip(ASCII_SPACE),
            notes=clean_line(values.get("notes", "")),
        )
        if row.blank:
            continue
        if not row.name:
            row.errors["name"] = "Give this ingredient a name."
        elif len(row.name) > INGREDIENT_NAME_MAX:
            row.errors["name"] = f"Ingredient names can be up to {INGREDIENT_NAME_MAX} characters."
        if len(row.amount) > AMOUNT_MAX:
            row.errors["amount"] = f"Amounts can be up to {AMOUNT_MAX} characters."
        if len(row.notes) > NOTES_MAX:
            row.errors["notes"] = f"Notes can be up to {NOTES_MAX} characters."
        _, calorie_error = parse_calories(row.calories)
        if calorie_error:
            row.errors["calories"] = calorie_error
        form.rows.append(row)

    if len(form.rows) > MAX_INGREDIENTS:
        form.errors["ingredients"] = f"A recipe can have up to {MAX_INGREDIENTS} ingredients."

    form.cover = parse_cover(data.get("cover", ""), set(photo_keys))
    return form


def parse_cover(value, owned):
    """A cover choice that can actually apply, or None to keep the default (the first photo).

    An existing photo must belong to this recipe; a new upload's index is checked against
    the files that were really stored, in the route.
    """
    match = COVER_RE.fullmatch(value or "")
    if not match:
        return None
    if match["key"]:
        return ("photo", match["key"]) if match["key"] in owned else None
    return ("new", int(match["new"]))


def fork_name_suggestion(parent_name):
    """"<parent> remix", trimmed to fit, and never equal to the parent's own name."""
    for suffix in (" remix", " remix 2"):
        suggestion = parent_name[: NAME_MAX - len(suffix)] + suffix
        if suggestion.casefold() != parent_name.casefold():
            return suggestion
    return suggestion


def parse_fork_name(raw, parent_name):
    """Return (name, error). The name must differ from the immediate parent's, ignoring case."""
    name = clean_line(raw)
    if not name:
        return name, "Give your remix a name."
    if len(name) > NAME_MAX:
        return name, f"Recipe names can be up to {NAME_MAX} characters."
    if name.casefold() == clean_line(parent_name).casefold():
        return name, "Must be different from the original name."
    return name, None


def parse_tags(raw):
    """Return (tags, error). Forgiving about case, separators, '#' and repeats; strict about letters.

    "Soup, #vegan  soup" -> ["soup", "vegan"]. Digits, dashes and accents are rejected, never
    silently stripped. Non-ASCII is rejected *before* lowercasing, because some non-ASCII
    letters lowercase into ASCII (the Kelvin sign "K" becomes "k").
    """
    raw = raw or ""
    if len(raw) > TAGS_FIELD_MAX:
        return [], f"The tags field can be up to {TAGS_FIELD_MAX} characters."
    tags = []
    for token in TAG_SEPARATORS.split(raw):
        token = token.lstrip("#")
        if not token:
            continue
        if not token.isascii() or not TAG_RE.fullmatch(token.lower()):
            shown = token[:TAG_MAX] + ("…" if len(token) > TAG_MAX else "")
            return [], f"“{shown}” isn't a tag. Use {TAG_MIN}–{TAG_MAX} letters a–z, like “soup”."
        tag = token.lower()
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        return [], f"A recipe can have up to {MAX_TAGS} tags."
    return tags, None


def canonical_tag(value):
    """The tag a ?tag= value means, or None if it can't be one. "  Soup " -> "soup"."""
    value = (value or "").strip(ASCII_SPACE)
    if value.isascii() and TAG_RE.fullmatch(value.lower()):
        return value.lower()
    return None


def parse_tag_search(values):
    """Read the search page's ?tag=a&tag=b. Returns (tags, problem).

    `tags` is the canonical selection: valid tags, deduplicated, sorted A-Z. `problem` is
    None, "invalid" (some value can't be a tag) or "too_many". On a problem the valid tags
    are still returned so the form can keep them ticked, but no search runs. Empty values
    are ignored, the way an unticked form submits.
    """
    values = [v for v in values if v.strip(ASCII_SPACE)]
    if len(values) > MAX_SEARCH_VALUES:
        return [], "invalid"
    tags, invalid = set(), False
    for value in values:
        tag = canonical_tag(value)
        if tag is None:
            invalid = True
        else:
            tags.add(tag)
    tags = sorted(tags)
    if invalid:
        return tags[:MAX_SEARCH_TAGS], "invalid"
    if len(tags) > MAX_SEARCH_TAGS:
        return tags[:MAX_SEARCH_TAGS], "too_many"
    return tags, None


def parse_sort(values):
    """The sort in ?sort=: "cal-asc", "cal-desc", or None for freshest first.

    The first value decides. Anything unknown means freshest first: a sort only reorders,
    so there's nothing to warn about, and the search page redirects the bad value away
    rather than ever showing it.
    """
    if values and values[0] in CALORIE_SORTS:
        return values[0]
    return None


@dataclass(frozen=True)
class TextSearch:
    """A ?q= search. `q` is the canonical text (shown in the box and used in URLs), or None
    for no word search. `words` are what's actually matched. `problem` is None,
    "too_long" or "too_many_words"; with a problem, nothing is searched."""
    q: str | None = None
    words: tuple = ()
    problem: str | None = None

    def match_expression(self):
        """The FTS5 MATCH string: every word quoted, so nothing typed can be FTS syntax.
        Words are runs of letters, digits and marks, so they never contain a quote."""
        return " ".join(f'"{w}"*' if len(w) >= PREFIX_MIN else f'"{w}"' for w in self.words)


def search_words(text):
    """The words in `text`: casefolded runs of letters, digits and combining marks, without
    stopwords, one-character words or repeats. Everything else just separates words."""
    words, current = [], []
    for ch in text.casefold() + " ":
        if unicodedata.category(ch)[0] in "LNM":
            current.append(ch)
        elif current:
            word = "".join(current)
            current = []
            if len(word) > 1 and word not in STOPWORDS and word not in words:
                words.append(word)
    return words


def parse_text_search(values):
    """Read ?q=. The first value decides; it's NFC-normalized, stripped of control characters,
    and its whitespace collapsed. Text with no searchable words means no word search."""
    text = " ".join(_drop_controls(unicodedata.normalize("NFC", values[0] if values else "")).split())
    if not text:
        return TextSearch()
    if len(text) > SEARCH_TEXT_MAX:
        return TextSearch(q=text[:SEARCH_TEXT_MAX], problem="too_long")
    words = search_words(text)
    if not words:
        return TextSearch()
    if len(words) > MAX_SEARCH_WORDS:
        return TextSearch(q=text, problem="too_many_words")
    return TextSearch(q=text, words=tuple(words))
