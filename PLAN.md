# Forkful — build plan (v3, after the beta plan and code reviews)

*Cook it. Fork it. Love it.*

A private recipe box for about 5 people. You can fork any recipe into a remix, and every recipe shows its parent and its forks.

v2 includes the findings from nine beta plan reviews (§15). v3 adds the fixes from nine beta code reviews (§16).

## 1. Requirements (from the user)

| # | Requirement | Where |
|---|---|---|
| R1 | Users are created from the command line, with no sign-up page | §6 |
| R2 | Log in and log out | §5 |
| R3 | Recipes are private to each user | §5 scoping |
| R4 | A recipe has a name, ingredients, instructions, photos | §3 |
| R5 | Each ingredient has a name, a free-text amount, and integer calories typed in | §3, §7 |
| R6 | Total calories are added up automatically | §3 |
| R7 | Instructions are one block of text | §3 |
| R8 | Multiple photos per recipe, **no limit on count** | §4 |
| R9 | Create a recipe | §7 |
| R10 | Edit as often as you like, with no versions | §7 |
| R11 | When editing, keep or remove photos and add new ones | §4, §7 |
| R12 | Fork a recipe into a new recipe with a new name | §8 |
| R13 | A fork starts with the parent's ingredients and instructions, but no photos | §8 |
| R14 | Forks can be forked | §8 |
| R15 | A fork remembers its parent | §3 |
| R16 | A recipe page shows its parent and its forks (one level each way) | §8, §9 |
| R17 | A list of all your recipes, forks included | §7 |
| R18 | No merging | none built |
| R19 | SQLite | §3 |
| R20 | Docker Compose, plain HTTP, **port 7777, public internet** | §10 |
| R21 | `set-password` CLI command | §6 |
| R22 | A fun, colorful, playful UI that's a joy to use | §9 |
| R23 | With several photos, the user can choose the cover (primary) photo; by default it's the first one uploaded | §17 |
| R24 | A recipe with no photos shows a generic default image | §17 |

**Not building:**
- recipe deletion
- sign-up
- email password reset
- sharing between users
- merging
- versions or history
- thumbnails or a gallery
- unique recipe names (only the fork rule in §8 applies)
- ingredient reordering
- search
- admin UI
- TLS
- a full multi-level family tree

## 2. Stack (pinned)

- **Python 3.14**, on the base image `python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8`.
- **Runtime libraries:**
  - Flask 3.1.3
  - Werkzeug 3.1.9
  - Jinja2 3.1.6
  - Waitress 3.0.2
  - Pillow 13.0.0
  - argon2-cffi 25.1.0
  - every dependency, transitive ones included, pinned with `==` and a sha256 hash, installed with `--require-hashes --only-binary=:all:`
- **Test:** pytest 9.2.0.
- **Database access:** stdlib `sqlite3`, raw SQL, no ORM.
- **Front end:** vanilla JS as progressive enhancement, with no build step and no third-party assets.
- **Waitress** with **4 threads** in one process.
  - It buffers request bodies, so slow clients don't hold app threads.
  - Running a single process lets login throttling live in memory.

## 3. Data model (`forkful/migrations/0001_init.sql`)

**Connections:** each request gets its own connection on `g`, closed in `teardown_appcontext`.

```python
db = sqlite3.connect(path, autocommit=True, timeout=5)   # explicit transaction control
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA busy_timeout=5000")
db.execute("PRAGMA synchronous=FULL")
```

- `journal_mode=WAL` is set once by `flask migrate`. It persists in the database file.
- **Writes:** every multi-statement write runs inside `with transaction(db):`, which issues `BEGIN IMMEDIATE`. On success it runs `COMMIT`. On any exception it runs `ROLLBACK` and re-raises.
- **Busy database:** a `sqlite3.OperationalError` such as "database is locked" turns into a friendly 503 page ("Someone's in the kitchen, try again").

**Timestamps:**
- There is one helper, `utc_ts()`, which returns `now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")`.
- `now` comes from `app.config["CLOCK"]`, so tests can control it.
- The same format is used for stored values and for comparison cutoffs.

```sql
BEGIN IMMEDIATE;

CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- ASCII-only by validation, so NOCASE is exact
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,                          -- sha256 hex of the 32-byte cookie token
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE recipes (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  parent_id    INTEGER CHECK (parent_id IS NULL OR parent_id <> id),
  name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  instructions TEXT NOT NULL DEFAULT '' CHECK (length(instructions) <= 50000),
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  UNIQUE (id, user_id),
  FOREIGN KEY (parent_id, user_id) REFERENCES recipes(id, user_id)   -- parent must have same owner
);
CREATE INDEX recipes_user_updated ON recipes(user_id, updated_at DESC, id DESC);
CREATE INDEX recipes_parent ON recipes(parent_id, user_id, created_at, id);

CREATE TABLE ingredients (
  id        INTEGER PRIMARY KEY,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  position  INTEGER NOT NULL,
  name      TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  amount    TEXT NOT NULL DEFAULT '' CHECK (length(amount) <= 100),
  calories  INTEGER NOT NULL DEFAULT 0
            CHECK (typeof(calories) = 'integer' AND calories BETWEEN 0 AND 1000000),
  UNIQUE (recipe_id, position)
);

CREATE TABLE photos (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused (see §16)
  recipe_id    INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  file_key     TEXT NOT NULL UNIQUE
               CHECK (length(file_key) = 32 AND file_key NOT GLOB '*[^0-9a-f]*'),
  content_type TEXT NOT NULL CHECK (content_type IN ('image/jpeg','image/png','image/webp')),
  width        INTEGER NOT NULL,
  height       INTEGER NOT NULL,
  byte_size    INTEGER NOT NULL,
  position     INTEGER NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX photos_recipe ON photos(recipe_id, position);

PRAGMA user_version = 1;
COMMIT;
```

- **Migrations:**
  - The runner, `flask migrate`, first sets WAL mode outside any transaction.
  - It then runs each `NNNN_*.sql` file whose number is above `PRAGMA user_version`, using `executescript`. The script carries its own `BEGIN IMMEDIATE … PRAGMA user_version = N; COMMIT;`. Under `autocommit=True`, `executescript` adds no implicit commit. On error the runner rolls back and exits non-zero.
  - Running it twice does nothing the second time.
  - A database whose `user_version` is higher than the newest known migration is refused, with a clear error.
- **Calorie total:** `COALESCE(SUM(calories), 0)`, never stored.
  - Each row is capped at 1,000,000, and a recipe can have at most 200 rows, so the total can't overflow.
  - Totals are shown with thousands separators.
- **The composite foreign key:**
  - It accepts a NULL parent.
  - `UNIQUE(id, user_id)` gives it the parent key it needs.
  - Its action is the default `NO ACTION`. Nothing is ever deleted anyway.
  - `parent_id` is set only at fork time and never changes after that.
  - A CHECK blocks a recipe being its own parent. Longer cycles can't happen, because a parent always exists before its child.

## 4. Photos

- **Accepted formats:** JPEG (including iPhone `MPO`, kept as its first frame), PNG and WebP.
  - The format comes from Pillow, never from the filename or the client's MIME type.
  - GIF and HEIC get the friendly error "Please use a JPEG, PNG, or WebP photo".
  - The file input uses `accept="image/jpeg,image/png,image/webp"`, never `image/*` or `image/heic`.
- **Limits:**
  - There is **no cap on photo count** (R8).
  - Safeguards:
    - 30 MB per file, measured server-side with `seek`/`tell`.
    - 200 MB per request in Flask (`MAX_CONTENT_LENGTH`).
    - 201 MB in Waitress (`max_request_body_size`), so Flask's friendly 413 page always renders.
    - `MAX_FORM_MEMORY_SIZE = 2 MB`.
    - `MAX_FORM_PARTS = 2000`.
    - `TMPDIR=/data/tmp`.
  - On a phone, adding photos across several saves goes beyond the per-request limit. The edit page says so.
- **Pipeline** (`photos.prepare(stream) -> (bytes, content_type, ext, w, h)`):
  - It runs under a process-wide `threading.Lock`, so only one decode happens at a time.
  - Every image is closed with a context manager.

  ```python
  im = Image.open(stream); fmt = im.format             # capture BEFORE exif_transpose
  if fmt not in {"JPEG","MPO","PNG","WEBP"}: reject
  if getattr(im, "n_frames", 1) > 1: im.seek(0)        # first frame only, never iterate frames
  w, h = im.size; if w*h > 60_000_000: reject "too large"   # explicit, before load
  if fmt in ("JPEG","MPO"): im.draft("RGB", (4096, 4096))   # cheap JPEG downscale while decoding
  im.load()                                             # real pixel decode (verify() does not decode)
  icc = im.info.get("icc_profile") if bytes and <= 1 MB else None   # keep color (Display P3)
  im = ImageOps.exif_transpose(im)                      # upright
  im = normalize(im)        # CMYK→RGB, I;16/I/F→L, P+transparency/PA/LA→RGBA, other→RGB
  im.thumbnail((4096, 4096))                            # cap the long edge at 4096px
  save as JPEG q88 (RGBA→RGB), PNG, or WebP q88, passing ONLY icc_profile (never exif/xmp/pnginfo)
  ```

  - `DecompressionBombWarning`, `DecompressionBombError`, `UnidentifiedImageError` and `OSError` all become the same friendly per-file error, which names the file.
- **Ordering and consistency on create and edit:**
  1. Validate all text fields first. If anything fails, re-render the form (400) with no file work done.
  2. Process the new files one at a time, writing each to `/data/photos/<file_key>.<ext>` and running `fsync` on the file and the directory. If any file fails, unlink the files already written in this request and re-render, naming the failed file.
  3. One transaction does all of the following:
     - updates the recipe row
     - replaces its ingredient rows
     - deletes the removed photo rows (posted `remove_photo` ids intersected with this recipe's photo ids)
     - inserts the new photo rows, with positions after `MAX(position)` read inside the transaction
  4. On any exception, roll back and unlink this request's new files.
  5. After commit, unlink the removed photos' files. If a file is already missing, log it and carry on.
- **No startup orphan sweep.** Files left by a crash are harmless, and a sweep could delete real photos after a mismatched restore.
- **Empty file parts** (filename `""`) are skipped.
- **Serving** (`GET /photos/<file_key>`):
  - `file_key` must match `[0-9a-f]{32}`.
  - The photo row is joined to its recipe, and the recipe's `user_id` must be the current user. Otherwise the response is 404.
  - The extension comes from the content type map, which is the same map used when writing.
  - The resolved path must sit inside `photos_dir`.
  - `send_file(path, mimetype=row.content_type, conditional=True, etag=True)`, then the headers are overwritten:
    - `Cache-Control: private, no-cache`, so every reuse goes back to the server, which means the auth check runs again. An unchanged photo gets a cheap 304.
    - `Vary: Cookie`
    - `X-Content-Type-Options: nosniff`
- **Forks never copy photos,** so a file always belongs to exactly one row.

## 5. Auth, sessions, privacy, hardening

The app runs over plain HTTP on the open internet, and the user has accepted that risk (§11). Everything below works without TLS.

- **Passwords:**
  - Argon2id (argon2-cffi defaults).
  - The CLI enforces at least 5 characters (10 until v1.7.1, lowered on request).
  - The dummy hash is generated at startup with the same parameters, so an unknown username still runs one verify.
  - Unknown usernames and wrong passwords return the same response.
- **Login input:**
  - The username is checked against the CLI pattern `^[A-Za-z0-9_.-]{1,32}$`, and the password is limited to 1024 characters, both before any lookup.
  - Bad input returns the generic failure and counts toward the IP limit.
- **Sessions:**
  - Flask's signed cookie holds only `{"sid": <raw token>, "csrf": <token>}` plus short fixed flash messages.
  - Cookie settings:
    - `SESSION_COOKIE_HTTPONLY=True`
    - `SESSION_COOKIE_SAMESITE="Lax"`
    - `SESSION_COOKIE_SECURE=False`, deliberately, because the app runs on HTTP
    - `session.permanent=True`
    - `PERMANENT_SESSION_LIFETIME=30 days`
  - The database stores `sha256(token)` with `expires_at`. Each request looks it up, and a missing or expired token means logged out.
  - **Login:**
    1. `session.clear()`.
    2. Create a new token and a new CSRF token.
    3. Delete expired session rows.
    4. Insert the new session with `INSERT INTO sessions … SELECT … FROM users WHERE id=? AND password_hash=?`, using the hash that was just verified.
    - If `set-password` changed the hash in the meantime, the insert affects 0 rows and the login fails.
    - After login the app always redirects to `/`. There is no `next=` parameter.
  - **Logout:** a POST that deletes the row and runs `session.clear()`.
  - **`set-password`:** updates the hash and deletes all of that user's sessions in one transaction.
- **CSRF:**
  - A token is created before the anonymous login form is rendered, and rotated at login.
  - Every POST, login included, must carry `csrf_token`. It is compared with `hmac.compare_digest`.
  - A missing, malformed or wrong token returns a friendly 400.
  - The CSRF check runs before the ownership check, so another user's id with a bad token gets 400, and with a good token gets 404.
- **Login throttle:**
  - It lives in memory, protected by a lock. A restart clears it.
  - Per-pair failures and per-ip failures are kept in two separate logs, so a flood in one can't evict the other.
  - Each log holds at most 10,000 keys and evicts the stalest first.
  - Usernames that fail the username pattern share a single `<invalid>` key, so a key never holds a large string.
  - Failures are counted in a 15-minute window:
    - 10 failures for the same (ip, username) pair returns 429.
    - 50 failures from one ip across all usernames returns 429.
  - A success clears that (ip, username) pair.
  - The (ip, username) key avoids the problem where anyone can lock a known user out from anywhere. If Docker ever hides the real client IP, it falls back to a per-username limit, which is the safe direction to fail.
  - The app logs `remote_addr` the first time it sees a login, so the operator can check whether real IPs are coming through.
  - The clock is injectable.
- **Scoping (R3):**
  - Every function in `repo.py` takes `user_id`. There is no fetch by id alone.
  - Reads and mutations include `user_id = ?`, or run inside a transaction after a scoped lookup.
  - Child ids from forms (photo ids to remove) are intersected with the scoped recipe's own ids.
  - Anything that isn't yours, or doesn't exist, returns **404**.
  - Every endpoint except `auth.login` and `static` is behind `login_required`. Logged-out users are redirected to `/login`.
- **XSS:**
  - Jinja autoescaping stays on (`.html` templates). There is no `|safe` or `Markup` anywhere near user data.
  - JS writes user-derived text only through `textContent`.
- **Headers on every response, error pages included:**
  - `Content-Security-Policy: default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin`
  - `X-Frame-Options: DENY`
  - `Permissions-Policy: camera=(), microphone=(), geolocation=()`
  - `Cache-Control: no-store` on HTML. Static files are cached for 1 hour. Photos are handled as in §4.
  - There are no inline scripts, inline styles, `style=` attributes or `on*=` handlers. A test enforces this.
- **Request size for small forms:** every POST that isn't multipart (login, logout, fork) is capped at 256 KB before the form is parsed. Werkzeug's per-field and part-count limits only apply to multipart bodies.
- **Secret key:**
  - `/data/secret_key` holds 64 random bytes, written as hex.
  - It is created atomically with `os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)`. If another process wins the race, the app reads that file instead.
  - The `SECRET_KEY` env var overrides it, but must be at least 32 characters or the app refuses to start.
- **Errors and logging:**
  - Friendly pages for 400, 404, 413, 429, 500 and 503.
  - Debug mode is never on.
  - Logs never contain request bodies, passwords, tokens or cookies.

## 6. CLI (Flask click commands; `FLASK_APP=forkful:create_app` is set in the image)

```sh
docker compose exec web flask create-user alice     # needs a TTY: prompts twice, hidden
docker compose exec web flask set-password alice    # prompts twice, then logs alice out everywhere
printf 'pw...\n' | docker compose exec -T web flask create-user alice --password-stdin   # scripts
docker compose exec web flask migrate               # also runs at container start
```

- Usernames must match `^[A-Za-z0-9_.-]{1,32}$` and are unique regardless of case.
- Passwords must be 10 to 1024 characters.
- A duplicate user, unknown user, invalid username or short password prints a friendly message on stderr and exits with code 1. No traceback is shown.

## 7. Pages, routes, forms

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/login` | Asks "Who's cooking today?" |
| POST | `/logout` | |
| GET | `/` | All your recipes, forks included. Ordered `updated_at DESC, id DESC`. Empty state when there are none |
| GET/POST | `/recipes/new` | Starts with 5 blank ingredient rows |
| GET | `/recipes/<int:id>` | The recipe page |
| GET/POST | `/recipes/<int:id>/edit` | Existing rows plus 3 blank ones |
| GET/POST | `/recipes/<int:id>/fork` | The standalone fork page. JS upgrades it to a `<dialog>` |
| GET | `/photos/<file_key>` | Serves a photo after the owner check |

**Form fields:**
- Ingredient rows use indexed names: `ing-<i>-name`, `ing-<i>-amount`, `ing-<i>-calories`.
- The server collects the indexes that appear in the form, sorts them numerically, and accepts at most 200 non-blank rows.
- Positions are assigned by the server as `0..n-1`.

**Normalizing text:**
- Names, ingredient names and amounts are NFC-normalized, have control characters removed, and are trimmed.
- Instructions are NFC-normalized and have `\r\n` converted to `\n`.
  - Control characters other than newline and tab are removed.
  - Blank lines at the start and whitespace at the end are trimmed.
  - Inner whitespace and the first line's indentation are kept.

**Validation** (Python checks the same limits as the database):
- **Recipe name:** required, 1–120 characters after normalizing.
- **Instructions:** optional, at most 50,000 characters.
- **Ingredient rows:**
  - A row with name, amount and calories all blank is dropped.
  - A row with an empty name but some other field filled is an error: "Give this ingredient a name".
  - The name may be up to 200 characters and the amount up to 100.
- **Calories:**
  - After trimming ASCII whitespace only, the value must match `CALORIES_PATTERN = 0|[1-9][0-9]{0,5}|1000000`.
    - The same pattern string is used by the Python check, the HTML `pattern` attribute and `forkful.js`.
    - A test keeps all three in sync.
  - A blank value means 0.
  - Anything else gets "Use a whole number from 0 to 1,000,000, like 120".
- **Ingredients are optional,** so a recipe can have zero. Photos are optional too.

**Errors:**
- The form re-renders with status 400 and keeps everything the user typed, including which photos were marked for removal.
- A `role="alert"` summary at the top lists every error, and each field shows its own error too.
- Newly chosen photo files can't be kept, and the summary says so.
- Native validation attributes (`required`, `maxlength`, `pattern`, `inputmode="numeric"`) catch most errors before the form is submitted, even with JS off.

**Saving:**
- **Create:** sets `created_at = updated_at = utc_ts()`.
- **Edit:** sets only `updated_at`, overwriting in place. If two tabs save, the last save wins (accepted; no versions).
- **After create or fork:** `flash("…", "celebrate")` runs before the redirect. The flash is used up the first time a page shows it, so the confetti plays once.

## 8. Forking

- **Name:**
  - Required, normalized as in §7, 1–120 characters.
  - It must differ from the **immediate parent's** current name, compared with `casefold()` after normalizing.
  - This rule applies on the fork form only. Siblings may share a name, and a fork may later be renamed to anything.
- **Prefill:** the field's value is `parent.name[:114] + " remix"`, editable and selected when the dialog opens. A hint below reads "Must be different from the original name."
- **Steps,** with `BEGIN IMMEDIATE` issued *before* the source is read:
  1. Load the source with `id=? AND user_id=?`, or return 404.
  2. Insert the new recipe: same `user_id`, `parent_id = source.id`, the new name, the copied `instructions`, and `created_at = updated_at = utc_ts()`.
  3. `INSERT INTO ingredients (recipe_id, position, name, amount, calories) SELECT ?, position, name, amount, calories FROM ingredients WHERE recipe_id = ?`.
  4. Copy no photos.
  5. Never write to the parent row.
- **The fork is a snapshot** of the parent at commit time.
- **Errors:** 400, re-rendering the fork page with the typed name and the error message.
- **Recipe page:**
  - The parent is loaded with `SELECT id, name … WHERE id = parent_id AND user_id = ?`.
  - Forks are loaded with `SELECT id, name … WHERE parent_id = ? AND user_id = ? ORDER BY created_at, id`.
  - All forks are shown.
  - Only one level is shown each way.

## 9. UI: playful, colorful, a joy to use

**Personality:** a sunny kitchen with a sticker book.
- Chunky ink outlines and candy accents.
- A little bounce, used sparingly.
- Silly in headings, empty states and toasts. Plain and clear on buttons and errors.

**Palette:**
- The page is cream paper `#FFF8EC`, the text is ink `#2B2118` (14.9:1), and cards are white `#FFFFFF`.
- Seven flavors, defined once as CSS custom properties. The confetti JS reads them with `getComputedStyle`.

| Flavor | Hex | Text on it | Signature pattern (CSS gradients only) |
|---|---|---|---|
| tomato | `#FF5A4E` | ink (5.1:1) | polka dots |
| tangerine | `#FF9F1C` | ink (7.7:1) | diagonal stripes |
| lemon | `#FFD23F` | ink (10.9:1) | gingham |
| mint | `#2EC4B6` | ink (7.3:1) | scallop waves |
| blueberry | `#3A86FF` | ink, **large/bold only** (4.5:1) | dashes |
| grape | `#8338EC` | **white only** (5.6:1) | sprinkle dots |
| bubblegum | `#FF66C4` | ink (6.0:1) | checkerboard |

- Text never goes white on tomato, tangerine, lemon, mint, blueberry or bubblegum.
- A test parses the CSS variables and checks every text and background pair against WCAG AA.
- **Flavors are mostly accents.** Cards stay white or cream.
  - Two places use a full flavor fill, with the safe text color from the table above: the emoji tile on a list card with no photo, and the "This recipe" chip.
  - Everywhere else, the recipe's flavor appears only in:
  - the washi-tape strip
  - the card's top border
  - the header band
  - the calorie stamp's ring
  - the family-tree chip outlines
- Every flavored surface keeps the 3px ink outline, because lemon and tangerine are nearly invisible on cream without it.
- **Assigning flavors and tilts:**
  - Each recipe's flavor class is `.flavor-{id % 7}`.
  - Its tilt is `.tilt-{id % 5}`, which maps to `-1.5deg`, `-0.75deg`, `0`, `0.75deg` or `1.5deg`.
  - The tilts form a closed set of classes, so no `style=` attribute is ever needed.

**Type:**
- Fredoka (SIL OFL, vendored as woff2 with `OFL.txt`) is used **only** for headings, recipe names and the stamp.
- Body text, ingredients and instructions use the system UI font stack.
- Instructions are 18px with a line height of 1.7.
- `overflow-wrap: anywhere` everywhere, so long words don't break the layout.

**Shapes:**
- 3px ink outlines.
- Hard `4px 4px 0` ink shadows.
- Rounded corners.
- Cards sit at a static tilt. Grid gaps leave room for the rotated outline, the shadow and the focus ring.

**Screens:**
- **Login:**
  - Headline "Who's cooking today?", with a 🍳 that wobbles once on load.
  - Fields for Username and Password, and a **Log in** button.
  - A failed login says "That username and password don't match. Check them and try again."
  - The 429 page says "Whoa, too many tries! Take a snack break 🍪 and try again in a few minutes."
- **My recipes:**
  - A "＋ New recipe" card comes first. On narrow screens it becomes a compact banner above the grid.
  - Each card shows:
    - its first photo in a polaroid frame, fixed height with `object-fit: cover`, and `loading="lazy"` after the first row
    - or, with no photo, a big food emoji chosen by id (🥞🍝🌮🥗🍜🍰🍕🥘🍲🥐)
    - the name
    - a round calorie stamp
    - "Updated <date>" in small text
    - for a fork, a one-line sticker reading "🌱 Remix of <parent>" that truncates with an ellipsis and shows the full name on hover
  - Every card is rendered on the server, so the browser's find-in-page works.
  - The empty state reads "Your recipe box is empty! Let's cook something up 🧑‍🍳".
- **Recipe page:**
  - A "← Remix of <parent>" text link appears above the title when there is a parent.
  - A header band shows the flavor pattern.
  - The calorie stamp reads "🔥 1,240 calories". It pops in once, then stays still.
  - Ingredients are listed on lined paper. Rows are at least 48px tall, each with a calorie pill (13px bold ink, flavor border).
  - Instructions sit on a recipe-card panel with `pre-wrap`.
  - Photos appear as polaroids with washi tape. Each links to the full image and shows a "Tap to enlarge" caption.
  - Buttons: **🍴 Fork it!** (a link to `/recipes/<id>/fork`, which JS upgrades to the dialog) and **✏️ Edit recipe**.
  - **Family:** `<nav aria-label="Recipe family">` contains:
    - "Parent recipe:" with a link
    - "This recipe" in `<strong aria-current="page">`
    - "Remixes of this recipe:" with a `<ul>` of links
    - Chips are outlined in each recipe's own flavor.
    - The dashed connector is decorative only, drawn with pseudo-elements and hidden from screen readers.
    - With no parent and no forks: "An original! Fork it to start a family 🌱".
- **New and edit form:**
  - Each ingredient row is its own group of three fields.
    - The first row has visible labels: Ingredient, Amount, Calories.
    - Later rows use `aria-label`, for example "Ingredient 3 name".
    - The calories field uses `inputmode="numeric" pattern="[0-9]*"`.
  - Each row has a ✕ button, at least 44px, labelled for screen readers, for example "Remove ingredient 3".
    - With JS on, ✕ removes the row.
    - With JS off, it isn't shown, and clearing a row's fields drops it on save.
  - The **＋ Add ingredient** button appears only when JS is on.
  - Focus moves to the new row's name field, or to the next row after a removal.
  - **Live total:** reads "Total so far: 1,240 calories". It uses the same calorie regex as the server, updates on input and is not announced to screen readers.
  - **Photos:**
    - A drop zone reads "Drop tasty pics here 📸 or tap to choose". It wraps the real file input, so it works with JS off.
    - With JS on, it shows `blob:` previews of new files and a total-size pre-check that warns above 200 MB.
    - Existing photos show as 96px thumbnails with `object-fit: cover`, `loading="lazy"` and `decoding="async"`.
    - Each has a real `remove_photo` checkbox, styled as a "Keep / Remove" toggle. A removed photo greys out with the label "Removed — will be deleted when you save".
    - A note reads "New photos are added at the end, in the order you pick them."
  - A sticky **Save recipe** bar uses `padding-bottom: calc(12px + env(safe-area-inset-bottom))`, and the form has a matching `scroll-padding-bottom`.
- **Fork dialog:**
  - A native `<dialog>` with the label "Name your remix", a prefilled value, a hint below the field, and the button **🍴 Fork it!**.
  - Esc closes it, and focus returns to the trigger.
  - The dialog submits the normal form. A server-side error renders the standalone fork page with the error shown.
- **Celebrations:**
  - The confetti only plays when you create or fork a recipe.
  - It's food emoji drawn on a canvas, shown for under 900ms.
  - The canvas has `pointer-events: none` and `aria-hidden`, and any key or tap dismisses it.
  - Toasts use `role="status"`. They slide in once, auto-dismiss after 4s and pause on hover or focus.
  - Success toasts: "Recipe created — looking delicious!", "Remix created — make it yours!", "Saved!".
- **Motion and accessibility:**
  - Hover straightening only applies under `@media (hover: hover) and (prefers-reduced-motion: no-preference)`. It is transform-only.
  - Focus shows a thick dashed ring and never moves the element.
  - `prefers-reduced-motion` turns off all animation, confetti included.
  - Every emoji has `aria-hidden="true"` and sits next to plain text that carries the meaning.
  - Tap targets are at least 44px.
  - Forms work fully with JS off.
- **Responsive:**
  - One column on phones.
  - The sticky save bar and the calorie stamp stay visible.
- **Assets:**
  - Fonts, CSS and JS are served only from `/static`.
  - Inline `<svg>` uses classes or `currentColor`, never a `style=` attribute.

## 10. Docker Compose

**`compose.yaml`:**

```yaml
name: forkful
services:
  web:
    build: .
    init: true
    ports: ["7777:7777"]                  # all interfaces, public internet (user decision)
    volumes: ["forkful-data:/data"]
    restart: unless-stopped
    stop_signal: SIGINT                   # waitress shuts down gracefully on SIGINT
    stop_grace_period: 30s
    mem_limit: 2g
    pids_limit: 256
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request as u; assert u.urlopen('http://127.0.0.1:7777/login', timeout=3).status == 200"]
      interval: 30s
      timeout: 3s
      retries: 3
      start_period: 15s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
volumes:
  forkful-data:
    name: forkful-data
```

**Dockerfile:**
- Base: `python:3.14-slim@sha256:…`.
- Environment: `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`, `FLASK_APP=forkful:create_app`, `FORKFUL_DATA_DIR=/data`, `TMPDIR=/data/tmp`.
- `pip install --no-cache-dir --only-binary=:all: -r requirements.txt`.
- `useradd --uid 10001 app`, then `mkdir -p /data/photos /data/tmp && chown -R app:app /data`.
- `COPY --chown=app:app`.
- `USER app`, so `docker compose exec` also runs as `app` and every file in `/data` has one owner.
- `ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]` with `CMD ["server"]`.

**`entrypoint.sh`:**
- Runs with `set -eu`.
- `server`:
  1. `mkdir -p "$FORKFUL_DATA_DIR/photos" "$TMPDIR"`
  2. `flask migrate`
  3. `exec waitress-serve --host=0.0.0.0 --port=7777 --threads=4 --max-request-body-size=210763776 --connection-limit=100 --channel-timeout=60 --call forkful:create_app`
- `flask …`: `exec flask "$@"`.
- Anything else: `exec "$@"`.

**`.dockerignore`:** `.git`, `tests`, `__pycache__`, `.venv`, `.pytest_cache`, `*.db*`, `data/`.

**README:**
- start and stop
- create a user and set a password
- **backup:** `docker compose stop`, then `docker compose run --rm --no-deps --entrypoint tar web czf - -C /data . > forkful-$(date +%F).tgz`, then `docker compose start`
- restore
- upgrade, including the rollback steps
- `docker compose down -v` **deletes all data**
- a fix for a volume owned by root
- the HTTP warning

## 11. Risk the user has accepted: plain HTTP on the public internet

Anyone who can watch the traffic can read passwords and steal session cookies. That includes public Wi-Fi, ISPs, and any network along the way.

§5 limits the damage:
- throttling
- logout that really ends the session
- `set-password` that revokes every session
- strict headers

None of it encrypts the traffic. HTTPS, for example a Caddy container with a domain, is not included because the user asked for HTTP. Users should not reuse a password from any other site.

## 12. Project layout

```
compose.yaml  Dockerfile  .dockerignore  entrypoint.sh
requirements.txt  requirements-dev.txt  README.md  PLAN.md
forkful/
  __init__.py     create_app(test_config=None): config, data dir, secret key, headers, errors, blueprints
  db.py           get_db(), transaction(), migrate(), utc_ts()
  migrations/0001_init.sql
  auth.py         login/logout, sessions, CSRF, throttle, login_required
  cli.py          create-user, set-password, migrate
  repo.py         all SQL, every function takes user_id
  forms.py        recipe and fork form parsing and validation (pure functions)
  recipes.py      list/new/show/edit/fork routes
  photos.py       prepare(), store, unlink, serve route
  templates/      base, login, list, show, form, fork, error, _macros
  static/css/forkful.css  static/js/forkful.js  static/fonts/
tests/
  conftest.py  factories.py  smoke.sh
  test_auth.py  test_privacy.py  test_csrf.py  test_recipes.py  test_forms.py
  test_photos.py  test_forks.py  test_cli.py  test_db.py  test_headers.py  test_templates.py
```

`create_app` creates no database connections and runs no migrations. Its only side effect is creating the secret key file.

## 13. Tests (pytest)

**Fixtures:**
- `create_app({"DATA_DIR": tmp_path, "CLOCK": fake_clock, smaller size limits})`.
- The database is migrated with the real `migrate`.
- `alice` and `bob` are created with the real `create-user` command through `CliRunner` and `--password-stdin`.
- `alice_client` and `bob_client` log in through the real form. Each GETs `/login` and parses out `csrf_token`. CSRF is never disabled in tests.
- `factories.py` provides:
  - JPEG, PNG and WebP images
  - a JPEG with EXIF orientation 6 and GPS data
  - a CMYK JPEG
  - an RGBA PNG and a palette PNG with transparency
  - a 16-bit PNG
  - an animated WebP
  - a GIF
  - a bomb PNG (tiny file, enormous IHDR)
  - a JPEG with trailing polyglot bytes
  - a text file named `.jpg`
  - an oversized file

**Tests by area:**
- **Census, taken from `app.url_map`:**
  - Every endpoint except login and static redirects an anonymous visitor to `/login`.
  - For every method and path filled in with alice's ids, bob gets 404.
  - Every POST endpoint rejects a missing or wrong CSRF token with 400.
  - A bad CSRF token on another user's id gets 400. A good token on another user's id gets 404.
  - `/signup` and `/register` return 404, and `/login` has no sign-up link.
- **Privacy:**
  - `/` lists only your own recipes.
  - Another recipe's `remove_photo` ids are ignored.
  - A raw SQL insert with a cross-user `parent_id` raises `IntegrityError`, and so does a self-parent.
- **Auth:**
  - Login and logout both work, and logout really kills the session.
  - Unknown users and wrong passwords get the same response.
  - The dummy verify runs once for an unknown user, checked with a call counter.
  - An expired session is treated as logged out.
  - Cookie flags: HttpOnly and SameSite=Lax are set, and Secure is absent on purpose.
  - Each login issues a new token, and the old session row is gone.
  - `set-password` kills existing sessions.
  - **Throttle:**
    - 10 failures for one (ip, user) pair → 429 at minute 14, cleared after 15 min.
    - 50 failures from one IP → 429.
    - A success resets the pair.
    - Usernames that fail the pattern don't crash.
- **Recipes:**
  - Create, edit repeatedly, no versions, and the last save wins.
  - The calorie total shows on the page, on the list card, after an edit and on a fork, and is 0 with no ingredients.
  - **Calorie input:**
    - Blank means 0.
    - Accepted: `0`, `1000000`.
    - Rejected: `1000001`, `-0`, `+5`, `1_000`, `٣`, `１２`, `1e3`, `0x10`, `1,000`, `2.5`.
  - Ingredient order is kept, and gaps in the indexes are fine.
  - A blank row is dropped, a partly blank row is an error, and more than 200 rows is an error.
  - Amount text round-trips ("1½ cups", "a pinch").
  - Every length limit, tested right at its boundary.
  - A missing name is rejected.
  - On re-render, typed values and photos marked for removal are kept.
  - Instructions keep their newlines, and HTML in them is escaped.
  - `updated_at` changes and the recipe moves to the top of the list.
  - **List page:**
    - forks appear, with their sticker
    - the empty state shows
    - a card shows its first photo, or an emoji when there is none
- **Photos:**
  - JPEG, PNG and WebP are accepted, and MPO is stored as JPEG.
  - A text file, GIF, bomb and oversized file are each rejected with no row and no file left behind.
  - Orientation 6 comes out rotated, with width and height swapped.
  - EXIF and GPS data are gone, but the ICC profile is kept.
  - CMYK is stored as RGB.
  - Transparency survives in PNGs.
  - A 16-bit image is stored as 8-bit.
  - An animated image becomes one frame.
  - Trailing polyglot bytes are gone.
  - The long edge is at most 4096.
  - The database's width, height and size match the file.
  - Keep, remove and add in one edit, with removed files really gone.
  - Removing all photos works.
  - 30 photos on one recipe works, so there's no count cap.
  - A forced failure during the insert or commit leaves no new files.
  - **Serving:**
    - the content type comes from the database row
    - the headers are `private, no-cache`, `Vary: Cookie` and `nosniff`
    - the ETag produces a 304
  - An empty file part is ignored.
  - The 413 page is friendly.
- **Forks:**
  - A fork of a parent with photos gets the ingredients and instructions, no photos, and `parent_id` set.
  - Editing the parent doesn't change the fork, and editing the fork doesn't change the parent.
  - The parent's `updated_at` is unchanged, and the fork is first on `/`.
  - A fork of a fork works.
  - The parent page lists its forks, and the fork page links to its parent.
  - A name matching the parent's is rejected, regardless of case or surrounding spaces.
  - The prefill fits within 120 characters.
  - A parent with zero ingredients can be forked.
- **CLI:**
  - create-user, a duplicate ("Alice" then "alice"), and invalid usernames (space, 33 characters, empty)
  - a short password
  - set-password, and an unknown user
  - exit code 1 with no traceback
- **Database:**
  - migrate on a fresh database
  - migrate twice does nothing
  - existing data survives a migrate
  - `user_version` 99 is refused
  - WAL mode is on
- **Headers:**
  - the exact CSP string, and every security header
  - present on 404 and 500 pages too
  - 500 shows no traceback
  - HTML is `no-store`
- **Templates and static files:**
  - no `style=`, no `on*=`, no inline `<script>` content
  - no `http(s)://` URLs to third parties
  - the flavor contrast check
- **Smoke test, `tests/smoke.sh`:**
  1. `docker compose up -d --build`
  2. Wait for the healthcheck.
  3. `create-user --password-stdin`.
  4. Use curl to log in with CSRF, create a recipe with a photo and fork it.
  5. Run `docker compose restart`, then `down` and `up` keeping the volume, and check the data each time.
  6. Check the CSP header.
  7. Check that a request over 200 MB gets the friendly 413.

## 14. Build order

1. Skeleton: `db.py`, migrations, the CLI, and their tests.
2. Auth: sessions, CSRF, throttle, headers, and tests.
3. Forms and recipes: create, show, edit, list, calories, and tests.
4. Photos and their tests.
5. Forking and the family display, and tests.
6. The UI pass: CSS, JS enhancements, confetti, copy, and the template and contrast tests.
7. Docker, the README and `smoke.sh`, run on port 7777.
8. Beta code review, then fixes.

## 15. What changed in v2 (from the beta plan review)

- **Bugs fixed in the plan:**
  - Create and fork never set `created_at`, so every insert would have failed (DeepSeek).
  - `exif_transpose` loses `.format`, which would have crashed on portrait phone photos (Grok).
  - Photo ids were reused alongside a 1-day cache (Sonnet).
  - `send_file` marks responses `public` whenever a max-age is set (Grok).
  - The Waitress and Flask 413 limits clashed (Sonnet, Grok, Kimi, MiniMax).
- **Security:**
  - Throttling moved to in-memory (ip, username) pairs, which stops anyone locking a user out (Terra, Sonnet, DeepSeek, Kimi).
  - Login is conditional on the password hash it verified, closing the race with `set-password` (Terra).
  - The CSRF token rotates at login (Terra).
  - The secret key is created atomically (Terra).
  - Caching moved to `no-store` for pages and `private, no-cache` with `Vary: Cookie` for photos (Terra, Kimi, Grok).
- **Database:**
  - Explicit `autocommit=True` transactions, with every write in one transaction (Sol).
  - `synchronous=FULL` and fsync for photo files (Sol).
  - `typeof()` checks that calories are integers (Sol).
  - One timestamp format, with id tie-breaks (Sol, DeepSeek).
  - The orphan-photo sweep was removed (Kimi, Sol, Terra, Sonnet, Grok).
- **Photos:**
  - MPO, CMYK, 16-bit, palette and animated images are handled. The ICC color profile is kept.
  - An explicit pixel cap, with one decode at a time.
  - The long edge is capped at 4096px and the per-file limit raised to 30 MB. **The user can veto this.**
  - The `accept` list is fixed for Safari, and empty file parts are ignored (Grok).
- **Forms:**
  - Indexed ingredient fields, at most 200 rows, a strict calorie pattern, NFC and casefold name rules, and Python limits that match the database (DeepSeek, Sonnet).
  - 5 or 3 blank rows so forms work with JS off (Muse, Kimi).
  - Werkzeug's form limits are set explicitly (Kimi).
- **UI:**
  - An exact contrast table, flavor colors used as accents, signature patterns, accessible family navigation, the fork dialog prefill, toned-down motion, plain-verb buttons and kitchen-friendly sizes (Muse, Sonnet).
- **Docker:**
  - The `/data` owner, `USER app`, `init` and SIGINT, an entrypoint that passes commands through, pinned volume and project names, a healthcheck, log rotation, `TMPDIR` on the volume, pinned digests and wheels, and a working backup procedure (MiniMax).
- **Tests:**
  - Route census, CSRF census, an injectable clock, rollback and file tests, migration tests, security-control tests, photo-quality tests, a template lint and the smoke script (GLM, Sonnet).

## 16. What changed in v3 (from the beta code review)

- **Data integrity:** photo ids use `AUTOINCREMENT`, so a stale "remove photo" left ticked in another tab can't delete a newer photo that reused the id (Sol).
- **Crashes that returned 500 instead of 400 or 404:**
  - a non-ASCII CSRF token (Terra, Sonnet)
  - recipe ids beyond SQLite's 64-bit range (Sol, DeepSeek)
  - NUL characters in names (DeepSeek)
  - a "remove photo" value with more than 4,300 digits (DeepSeek)
- **Memory exhaustion:**
  - Throttle keys are bounded in size, and the per-pair and per-ip logs are separate (Terra, Sonnet, Kimi).
  - Urlencoded POST bodies are capped at 256 KB (DeepSeek).
- **Photos:**
  - A PNG `tRNS` color key now becomes real alpha (Grok).
  - The one-hour `Expires` header is removed from photo responses (Grok).
  - Every unreadable format gets the same message (Sonnet).
- **UI and accessibility:**
  - `[hidden]` now works, so dead buttons no longer show with JS off (Sonnet).
  - `overflow-wrap` applies to every element (Muse).
  - The photo-size warning is announced and focused, and photo errors are linked to the input.
  - Error styling applies only to the field that is wrong.
  - Focus behaves sensibly when rows are removed, and toasts can be focused.
  - Modified clicks on "Fork it!" open the fork page in a new tab.
  - Breadcrumb links are 44px tall, and screen readers announce "opens in a new tab" on photo links.
  - The calorie pattern is tightened, and the error-summary wording no longer assumes photos were chosen (Muse).
- **Forms:**
  - Error re-renders restore the full set of blank rows (DeepSeek).
  - Python and JS trim exactly the same whitespace (DeepSeek).
  - Instructions keep their first-line indentation (Sonnet).
  - The fork suggestion never equals the parent's name (Sonnet, DeepSeek).
- **Operations:**
  - The app logs at INFO, so the first-login IP line actually appears (MiniMax).
  - The dummy password hash is built at startup, and the first-login flag is kept per app (Kimi).
  - The healthcheck makes an HTTP request (MiniMax).
  - The dev server binds to 127.0.0.1 (MiniMax).
  - Backups are written atomically and leave out `tmp/`, and restore checks the archive before wiping anything (MiniMax).
  - `smoke.sh` never runs `down -v` and refuses to run unless its own override volume is active (MiniMax).
  - Dependencies are installed with hash checking (MiniMax).
- **Tests:** 175 became 206. The new tests cover every fix above, plus the mutation gaps GLM found:
  - malformed logins count toward the ip limit
  - list-card imagery and flavor classes
  - `no-store` on error pages
  - the 30-day session lifetime
  - a photo whose file is missing returns 404
  - expired sessions are cleaned up

## 17. Cover photo and default image (R23, R24; added after v3)

- **The cover is the recipe's first photo by `position`.**
  - No schema change is needed. The list card already shows the first photo, and the recipe page shows photos in `position` order, so the cover always leads.
  - New photos are appended, so on a recipe with no photos the first upload becomes the cover automatically. Later uploads don't take over.
  - Choosing a cover moves that photo to the front, setting `position = MIN(position) - 1` (`repo.set_cover`, scoped by `user_id`).
  - Removing the cover simply promotes the next photo.
- **Choosing a cover:**
  - The form sends `cover` as either `photo-<id>` (an existing photo) or `new-<n>` (the n-th file uploaded in this save).
  - It's applied in the same transaction as the rest of the save, after new photos are added.
  - It's ignored, and the default kept, when the value is malformed, the photo isn't this recipe's (for example, it was deleted in the meantime), or the index is past the files actually stored.
- **Edit page:** each existing photo has a **⭐ Cover** radio and a **🗑️ Delete** button. The chosen cover glows lemon.
- **Delete** (v1.2.0, replacing the earlier "Remove, deleted when you save" checkbox, which users found confusing):
  - `POST /recipes/<id>/photos/<photo_id>/delete` deletes one photo right away. It's scoped by `user_id` and recipe, and anything else returns 404.
  - It bumps the recipe's `updated_at` and unlinks the file after commit.
  - **With JS:** the browser asks for confirmation, then calls the endpoint with `fetch`, which returns 204. The tile disappears and any unsaved edits on the page are kept. If the deleted photo was the cover, the next tile's Cover radio is checked, matching what the server does. A "Photo deleted." toast confirms it.
  - **Without JS:** the button submits the form to the same URL (`formaction`), which then redirects back to the edit page.
- **New uploads (JS on):** each preview gets its own Cover radio.
  - On a recipe with no photos, the first preview is pre-selected, matching the server default.
  - With JS off, new photos follow the default, and the cover can be changed after saving.
- **Recipe page:** the first polaroid carries a "⭐ Cover" sticker when there is more than one photo.
- **Default image:**
  - `static/img/default-recipe.svg` is a covered dish (cloche) on lemon gingham, with steam and sparkles, in the app palette.
  - It's used on every list card without photos. That includes a new fork, since forks start with no photos.
  - It replaces the earlier emoji-on-flavor tile.

## 18. Version in the footer

- Every page's footer shows `Forkful v<version> · built <UTC time>`.
- The version is `forkful.__version__`, currently `1.2.0`. Bump it on every release.
  - 1.0.0 was the first release.
  - 1.1.0 added cover photos.
  - 1.2.0 replaced remove checkboxes with a Delete button and added cache-busting.
- The Docker build writes the build time to `forkful/BUILD`, which is git-ignored. Any code change re-runs that step, so the stamp shows which build the container is running.
- Running from a checkout without Docker, the footer says "development build".
- **Cache-busting:**
  - Templates link static files through `static_url()`, which appends `?v=<build stamp>` in Docker, or `?v=<file mtime>` in development.
  - A rebuilt app can therefore never be shown with a stale cached stylesheet or script.
  - The font preload keeps its plain URL, because it has to match the URL the CSS requests.

## 19. Mobile (v1.3.0)

This was checked with an automated audit in a phone emulation (touch, mobile Safari user agent) at 360 and 390px wide, on every page. It checks for:
- horizontal overflow
- tap targets under 44px
- inputs under 16px, which make iOS zoom on focus

Every page passes. At ≤560px:
- **Header:** one row, 63px tall instead of 177px. It has the logo (which goes home), "＋ New" (still announced as "New recipe" to screen readers) and Log out. "My recipes" and the name chip are hidden.
- **Recipe list:** compact cards, with a small tilted polaroid beside the name, remix sticker and calorie stamp, so about three recipes fit per screen instead of one. The dashed "New recipe" card is hidden, because the header button covers it.
- **Recipe page:** the calorie stamp becomes a corner sticker on the title card, Fork it and Edit share one row, and the ingredients start on the first screen. Photos sit in two columns and open in the same tab, so the back button returns.
- **Edit form:** two photo tiles per row, with 44px Cover and Delete controls, and tighter ingredient rows. Toasts sit above the sticky save bar instead of covering it.
- **At every size:**
  - Every control is at least 44px.
  - `touch-action: manipulation` removes double-tap zoom delays.
  - The fixed background turns off on touch devices, where it scrolls badly.

## 20. Tags (v1.4.0)

Request: "tags on recipes, lowercase a-z only, no spaces; find recipes by tag on the home page."

The design came from the beta team's input. Votes are noted where they split.

- **Storage** (`migrations/0002_tags.sql`):
  - One table, `recipe_tags(recipe_id, user_id, tag, position)` (6 votes vs. 3 for a separate tag dictionary).
  - The composite FK `(recipe_id, user_id) → recipes(id, user_id)` means a tag can only sit on its owner's recipe.
  - CHECKs enforce:
    - 2–24 letters a-z, via `NOT GLOB '*[^a-z]*'`
    - a byte length equal to the character length, which closes the NUL loophole, since SQLite's text functions stop at NUL
    - at most 10 tags, through `position` 0–9 being unique per recipe
  - Index `(user_id, tag, recipe_id)`.
- **Rules** (`forms.parse_tags`):
  - Forgiving:
    - ASCII case is lowercased
    - commas and whitespace both separate
    - a leading `#` is dropped
    - repeats collapse, and the first occurrence keeps its place
  - Strict: digits, dashes, accents and anything else are rejected with "“x” isn't a tag. Use 2–24 letters a–z, like “soup”." Nothing is silently stripped.
  - Non-ASCII is rejected *before* lowercasing, because the Kelvin sign lowercases to "k".
  - The raw field is capped at 300 characters.
- **Form:** one "Tags (optional)" field under the recipe name.
  - It sets `autocapitalize="none"`, `autocorrect="off"` and `spellcheck="false"`.
  - The edit form prefills it as "a, b, c".
  - Errors re-render with a 400 and keep what was typed.
- **Forks copy tags** (9/9). The fork dialog says so.
- **Home page:**
  - `/?tag=soup` shows one tag at a time, freshest first.
  - A tag bar shows "All" first, then the user's tags A–Z with counts. The active chip links back to `/`.
  - The bar is hidden until the user has tags.
  - Non-canonical URLs (`?tag=Soup`, repeated `tag` parameters) redirect to the canonical one.
  - An unknown tag shows "No recipes tagged “x”". An invalid one shows "That isn't a tag" and is never echoed back.
  - Both return 200 (7 vs. 2). Another user's tag is indistinguishable from a made-up one.
- **Display:**
  - The recipe page shows tappable tag chips under the title band.
  - List cards show plain labels (the card is already a link): 3 plus "+N", or 2 plus "+N" on phones.
  - The list page loads all card tags in one query.
- **Phones:** the tag bar is one swipeable row instead of wrapping (my call over the 6–3 vote for wrapping, to keep recipes on the first screen). JS scrolls the active chip into view.
- **Not built:**
  - multi-tag AND/OR
  - a search box
  - autocomplete
  - tag rename, merge or management pages
  - tag colors
  - `/tags/<name>` routes

## 21. Searching by several tags (v1.5.0)

Request: "select multiple tags and do the search." The design came from beta input, with votes noted.

- **Matching: all of the picked tags** (8 of 9). There's no AND/OR switch. "soup + vegan" means vegan soups.
- **Picking: checkbox chips in a GET form with a Search button** (7 of 9).
  - Tick several tags, then press **Search**. It works the same with JS off.
  - JS doesn't auto-submit (4 vs. 2). It only shows "Search (N)" as you pick, disables further chips at the 10-pick limit, and scrolls the first ticked chip into view on phones.
- **URL:** `/?tag=a&tag=b`. Values are canonicalized, deduplicated and sorted A–Z, and anything else 302s to that form. This replaces v1.4's "repeated `tag` → the first one" rule. One-tag links are unchanged.
  - **Limits:** at most 10 tags (8 of 9), so a search with 11 shows "Pick up to 10 tags". More than 32 values in the URL are refused outright.
- **A value that isn't a tag** shows "That isn't a tag" (6 of 9).
  - No partial search runs, and the value is never echoed back.
  - Valid picks stay ticked so the search can be fixed.
  - An unknown tag, or another user's tag, just matches nothing, and looks the same either way.
- **Counts stay totals** (4 vs. 5 for faceted counts; my call). With pick-then-Search, faceted counts would describe the *previous* search while you pick. Disabling zero-result chips would also block swapping one tag for another in a single search.
- **Feedback:**
  - A summary line reads "3 recipes with all of #dinner + #spicy, freshest first."
  - A tray holds one removable chip per tag, each a link to the search without that tag, plus "Clear all".
  - The page `<title>` names the search.
  - There are separate empty states for no match, an invalid value and too many tags.
- **Accessibility:**
  - Native checkboxes inside a `<fieldset>` whose legend says recipes must have every tag. No `aria-pressed`.
  - "All" keeps `aria-current="page"` when there's no search.
  - Focus rings show on the chip.
- **Phones:** the chip row stays one swipeable row (the hidden checkboxes sit inside its positioned row, so nothing overflows). A full-width Search button sits under it, and the search tray wraps above it.
- **SQL:** `r.id IN (SELECT recipe_id FROM recipe_tags WHERE user_id = :user AND tag IN (:t0, …) GROUP BY recipe_id HAVING COUNT(*) = :n)`. Placeholders are generated and every value is bound. The existing `(user_id, tag, recipe_id)` index covers it.
- **Not built:**
  - OR, NOT or a toggle
  - faceted counts
  - auto-submit
  - a free-text search box
  - saved searches

## 22. A search page, and sorting by calories (v1.6.0)

Request: "make a search page that has the tags search on it, also we need a sort by calories." The design came from beta input (9 of 9 answered). Votes are noted where they split.

- **Pages:**
  - **`/search`** (endpoint `recipes.search`) is the only place to search and sort.
  - **`/`** is back to a plain box: the New card plus every recipe freshest first, and a "🔍 Search & sort" button in the page head. It has no tag bar and no sort control (9/9).
- **No tags picked:** `/search` lists every recipe (9/9), so the calorie sort works on the whole box. The sort control shows even when the user has no tags yet, along with a "No tags yet" hint. An empty recipe box shows the usual empty state and no form. Results never include the New card.
- **Sorts:** three radio chips in a "Sort by" fieldset:
  - 🕒 Freshest (the default)
  - 🥗 Fewest calories
  - 🍔 Most calories

  The labels were my call between playful "Lightest/Heartiest" (5) and plain "Calories: low to high" (4). They name calories outright and keep the emoji. No name A–Z and no oldest sort.
- **One form, one button** (8/9): the tag checkboxes and sort radios share the GET form, and **Search** applies both. There's no auto-submit (9/9), so unsubmitted ticks are never lost or half-applied. It works the same with JS off.
- **URL:** `/search?tag=a&tag=b&sort=cal-asc`. Tags come first, sorted and deduplicated, then the sort.
  - The default sort stays out of the URL (7/9). The form submits `sort=new`, which 302s to the shorter URL.
  - An unknown, blank or repeated `sort` 302s to the canonical URL. The first value decides, and anything unknown means freshest. The bad value never appears in the page.
  - Tag rules are unchanged from §21. A bad tag still shows "That isn't a tag" with a 200, the sort falls back to freshest, and nothing is echoed.
- **Old links:** `/?tag=…` and `/?sort=…` 302 to `/search` (9/9 for 302 over 301/308). It's one hop when they're valid. Invalid values are forwarded unchanged so the search page explains them. Recipe-page tag chips link to `/search?tag=x`.
- **Recipes totalling 0 calories stay where 0 sorts** (6 vs. 3 for pushing them last). The stamp says 0, so the order matches what you see. There's no "unknown" concept and no label.
- **Results:**
  - Same cards, and no rank numbers (7/9).
  - When sorting by calories, the card stamps step forward: a little larger, on lemon (6/9).
  - The summary line says the order: "3 recipes with all of #dinner + #spicy, most calories first." / "All 10 recipes, fewest calories first."
  - Each ✕ in the tray, "Clear tags" (renamed from "Clear all"), the "All" chip and "Show all recipes" keep the current sort.
- **Header:** a Search link between "My recipes" and "＋ New".
  - On wide screens it shows "🔍 Search". On phones it's a round 44px 🔍 button with `aria-label="Search recipes"`, since the phone header already hides "My recipes" (§19).
  - Both links carry `aria-current="page"` on their own page. The "All" chip uses `aria-current="true"`, so the page has only one "page".
- **Phones:** the tag chips stay one edge-to-edge swipeable row. Sort chips wrap. Search is full width under both.
- **SQL:** `repo.list_recipes(…, sort=)` orders by the existing correlated `total_calories` alias.
  - The `ORDER BY` text comes only from the `repo.ORDER_BY` dict, and an unknown key raises.
  - Ties always fall back to `updated_at DESC, id DESC`.
  - No schema change, no new index, no join rewrite.
- **Not built:**
  - sort on the home page
  - auto-submit
  - name or oldest sorts
  - calorie ranges
  - hiding zero-calorie recipes
  - ranks
  - a remembered sort
  - a free-text search box
  - pagination

## 23. Searching by words (v1.7.0)

Request: "also do full text search on ingredients and instructions." The design came from beta input (9 of 9 answered). Votes are noted where they split.

- **What's searched:**
  - ingredient names
  - instructions
  - the recipe name (5 vs. 4; my call to follow the majority, because a box that can't find "Shakshuka" by its name feels broken)

  Not searched: amounts ("2 cups" is noise) and tags (the chips already do that).
- **Engine:** SQLite FTS5, migration `0003_recipe_text.sql`, schema version 3.
  - One row per recipe (rowid = recipe id) with columns `name`, `ingredients`, `instructions`.
  - Tokenizer: `porter unicode61 remove_diacritics 2` (stemming 6 vs. 3). So "tomatoes" finds "tomato", "roasted" finds "roast", and "creme" finds "crème".
  - Not trigram.
- **Kept in sync by triggers** (8 vs. 1):
  - Every insert, text update or delete on `recipes` or `ingredients` rebuilds that recipe's row from the base tables. That covers create, edit, fork, a future recipe delete, and raw SQL.
  - A rebuild of a recipe that no longer exists inserts nothing, so cascades can't resurrect it.
  - Touching `updated_at` (photo changes) doesn't reindex.
  - The migration back-fills every recipe.
  - Cost: each ingredient row rebuilds the doc, measured at about 20 ms for 30 ingredients and about 0.2 s for the absurd worst case of 200 ingredients with 50 KB of instructions.
- **No `user_id` in the index:** the search is `r.id IN (SELECT rowid FROM recipe_text WHERE recipe_text MATCH :match)` inside the usual `r.user_id = :user` query, so ownership has one home.
- **Query rules** (`forms.parse_text_search`):
  - Every word must match, across any of the three fields, AND-ed with tags (9/9).
  - There's no phrase, OR or exclusion syntax. Words are runs of letters, digits and marks. Everything else (quotes, `*`, `-`, `:`, brackets) only separates words, and each word is sent as a quoted string. Typed text can never be FTS syntax, and it's always a bound parameter.
  - Words of 3 or more letters also match as a prefix (6 of 9 wanted prefixes): "toma" finds "tomato". Shorter words must match whole.
  - The trade-off: "egg" also finds "eggplant".
  - Stopwords (a, an, and, or, the, of, to, in, on, for, with) and one-character words are ignored, so "chicken and rice" doesn't require "and".
- **Limits:** 200 characters and 10 words. Over a limit, there's no search, a friendly 200 page, and the text is kept in the box (cut to 200) so it can be fixed (6 vs. 2 for silent truncation). Input with no words (punctuation, emoji, only stopwords) means no word search, and its URL redirects without `q` (6 vs. 3).
- **No "Best match" sort** (7 vs. 2). The three sorts stay, and freshest stays the default. This also avoids bm25's corpus-wide statistics, a small cross-user signal.
- **No snippets or highlighting** (7 vs. 2), so there's no `|safe` anywhere.
- **URL:** `/search?q=garlic+tomato&tag=soup&sort=cal-asc`.
  - `q` is NFC-normalized, its control characters dropped and its whitespace collapsed. Case and punctuation are kept as typed, and the first value decides.
  - Anything else 302s to the canonical form. Empty `q` drops out.
  - `q` is the user's own text, so it *is* shown, in the box, the tray and the title, always through autoescaping. The summary shows the words actually searched.
- **UI:**
  - A "Search words" box comes first in the search form. Its hint reads "Looks in names, ingredients and instructions. Every word must match."
  - It uses `type=search` and `enterkeyhint=search`, with no autocapitalize, autocorrect or spellcheck, and no autofocus.
  - `input[type=search]` joined the 16px input rule so iOS doesn't zoom.
  - The tray gets a mint "words ✕" chip (7.3:1 contrast). Tag ✕ links and "All" keep the words, and "Clear search" clears words and tags but keeps the sort.
  - Summary: "3 recipes mentioning “garlic” + “onion” with #dinner, fewest calories first." Separate empty states cover no match, too long and too many words.
  - No box on the home page.
- **Tests:** the app fixture checks after *every* test that the index equals each recipe's text, computed in Python from the base tables, plus FTS5's integrity check. Every write path any test takes is therefore drift-checked.
- **Not built:**
  - relevance sort
  - snippets
  - phrases, OR or NOT
  - autocomplete or live search
  - typo tolerance
  - searching amounts or tags as text
  - a home-page box
  - per-user indexes
  - a reindex command (the triggers make it unnecessary)

## 24. Small requests (v1.7.1–v1.8.0)

- **v1.7.1:** passwords need at least 5 characters (was 10). The demo account's password was then changed directly, on request, bypassing the CLI check.
- **v1.7.2, no animations:**
  - Removed: confetti, toast slide/fade, the stamp drop-in, the login wobble, and the hover lift or tilt on buttons, cards and chips.
  - Hover underlines on nav, "Log out" and crumbs are gone too.
  - A global `animation: none; transition: none` rule stays as a backstop, and `tests/test_templates.py` fails if keyframes, transitions, hover motion or hover underlines come back.
  - The 🎉 "Recipe created" toast stays; it just doesn't move.
- **v1.8.0:**
  - The home page's heading block ("My recipe box", the count, "Search & sort") is gone; the page opens straight onto the cards and keeps a screen-reader-only `<h1>`.
  - The header's Search link became a grape button labelled "🔍 Search" (icon-only, 44px, on phones, so the 360px header stays one row).
  - The recipe page's photos moved into the hero band as taped polaroids between the title card and the buttons, cover first and a little larger, scrolling sideways when there are many. The separate Photos section is gone.
  - With no photos, the hero shows the default cloche image captioned "No photos yet".

## 25. Public recipe ids (v1.9.0)

Request: "instead of integers for recipe ids, can we use uuids or their shortened version." The design came from beta input (9 of 9 answered), with votes noted.

- **Internal ids stay; a public id is added** (9/9).
  - `recipes.id INTEGER PRIMARY KEY` remains the key for every foreign key, the ownership composites, the FTS rowid, ordering and the flavor/tilt colors.
  - A new `recipes.public_id` is the only recipe identifier in URLs, links, redirects and form actions.
- **Format:** 32 lowercase hex characters, i.e. 128 random bits from `secrets.token_hex(16)`. That's a UUID's worth of randomness without its dashes, and the same shape as photo file keys.
  - Hex won 5 of 9 (base64url 2, base32 2), and 6 of 9 wanted the full 128 bits.
  - Hex is one case only, double-clicks as one word, needs no escaping, and can be generated in SQL for the back-fill.
- **Migration `0004_public_ids.sql`** (schema 4), with no table rebuild:
  - `ADD COLUMN public_id`, back-filled with `lower(hex(randomblob(16)))`, plus `CREATE UNIQUE INDEX`.
  - A `BEFORE INSERT` trigger rejects NULL, non-text, a wrong length, a NUL (byte length), and anything but `[0-9a-f]`.
  - A `BEFORE UPDATE` trigger rejects any change; a public id is immutable.
- **Generation:** `repo.insert_recipe` (so forks too) generates the id in Python. It retries up to 3 times only on a `recipes.public_id` unique conflict, and returns `(id, public_id)`.
- **Routing:**
  - A `key` converter (`forkful/ids.py`, regex `[0-9a-f]{32}`) is used for `/recipes/<key:public_id>`, `/edit`, `/fork` and `/recipes/<key:public_id>/photos/<key:file_key>/delete`.
  - Integers, uppercase, the wrong length and trailing slashes don't match, so they're plain 404s.
  - Its `to_url` raises on anything else, so building a link from an integer id fails loudly instead of rendering one.
  - `owned_recipe_or_404` looks up `public_id AND user_id` in one query, so another user's recipe returns a 404 byte-identical to a nonexistent one.
- **Old integer URLs are 404s** (8 vs. 1), with no redirect or legacy routes. Bookmarks from before v1.9 need refreshing.
- **Photos:** deleting a photo goes by its `file_key` (9/9), scoped to the recipe and its owner. The edit form's cover radios are `photo-<file_key>` (7 vs. 2), so no internal id reaches the page.
- **Colors** stay derived from the internal id (8 vs. 1). The class only shows `id % 7`, and existing recipes keep their look.
- **What it buys:** URLs, history, logs and screenshots no longer reveal how many recipes exist or their order, and ids can't be enumerated or reused. Access control is unchanged: ownership checks remain the boundary, and a public id is not a share token.
- **Tests:**
  - The privacy census builds every route's URL with the app's own converters.
  - `tests/test_public_ids.py` covers the format, randomness, redirects, a scan for integer ids in every page, the loud failure, collision retry and give-up, and migrating a populated v3 database.
  - `tests/test_db.py` covers the triggers.
- **Not built:**
  - a primary-key swap
  - UUID types or libraries
  - case-insensitive or legacy routes
  - slugs
  - share links
  - id rotation

## 26. Shorter, NanoID-style public ids (v1.10.0)

Request: the user chose NanoID-style ids, "lowercase and digits, short": 12 characters such as `x4k9m2q7w3rt`.

- **Format:** 12 characters from `0-9a-z`, drawn with `secrets.choice` (36^12, about 62 bits). This is NanoID's method with a single-case alphabet, so there's no mixed case and no `-` or `_` to split a double-click selection. No library is needed.
  - Collisions: the unique index rejects them and `insert_recipe` retries (up to 3 tries). At a few hundred recipes per user the odds are about 1 in 10^13 per insert.
  - Guessing: ownership 404s remain the lock. The id only has to stop counting and enumeration.
- **Migration `0005_short_public_ids.sql`** (schema 5):
  - It drops v4's two triggers and re-issues every recipe's id in SQL: 12 × `substr(alphabet, (random() % 36 + 36) % 36 + 1, 1)`, avoiding `abs()` and its one overflow value.
  - It recreates the triggers for the new shape: exactly 12 characters, byte length 12, and `[0-9a-z]` only. Ids are immutable again.
  - v1.9's 32-hex links are 404s. Fresh installs run 0004 then 0005.
- **Routing:** two strict converters (`forkful/ids.py`): `<rid:public_id>` (`[0-9a-z]{12}`) for recipes and `<key:file_key>` (`[0-9a-f]{32}`) for photo file keys, which are unchanged.
- **Tests:** formats switched, plus tests for upgrading a populated v4 database (50 recipes; all new ids unique and none kept), 32-hex rejected afterwards, immutability, and the generator using the whole alphabet. Checked by hand with a 2,000-recipe upgrade: all unique, and character counts even (616–711 against 667 expected).

## 27. A Random button (v1.11.0)

Request: "in the menu bar, make a random button that will randomly choose a recipe."

- **Header:** a mint "🎲 Random" button between Search and "＋ New recipe" (`aria-label="Random recipe"`).
- **Route:** `GET /random` 302s to one of *your* recipes, chosen by `ORDER BY public_id IS :skip, random() LIMIT 1`.
  - The header sends `?skip=<public id>` from a recipe's page, edit page or fork page, so pressing it again always moves somewhere new. With only one recipe it goes there anyway.
  - A `skip` that isn't a well-formed id is ignored, and it can only ever reorder your own recipes.
  - With no recipes, it goes to `/` with the note "No recipes to pick from yet. Create one first!"
  - It's a GET with no state change, so it needs no CSRF token. Login is required, and the census covers it.
- **Header fit:**
  - Below 960px, Search and Random become 44px icon buttons, with names kept in their `aria-label`s, so tablets stay one row.
  - At 420px and below, the "Forkful" wordmark is visually hidden (screen readers still read it), and the 🍴 logo is a 44px home link. That way the logo, 🔍, 🎲, "＋ New" and "Log out" fit one 63px row on 360px phones.
  - 600–700px still wraps to two rows, as it did before.
- **Tests (`tests/test_random.py`):** it reaches every recipe and never another user's, skip moves on, one recipe still works, a bad skip is ignored, an empty box goes home with the note, the header link carries skip, and login is required.

## 28. A shorter recipe hero (v1.11.1)

Request: the hero band with the photos was "taking up way too much vertical space" (about 575px on desktop).

- **Wide screens (821px and up):** the band is a two-column grid. The title card and the Fork/Edit buttons sit on the left and the photos on the right, so the band is only as tall as its tallest column.
  - The calorie stamp becomes a corner sticker on the title card (as on phones), giving the title the card's width.
  - The title scales with `clamp(1.6rem, 1.2vw + 1.3rem, 2.4rem)`.
  - The photo column is capped at `min(35rem, 44vw)`, so three photos fit at 1280px; more scroll sideways.
- **Polaroids are smaller and all one size:** 9.5rem on desktop and 7.5rem on phones. The cover is no longer bigger; its ⭐ badge marks it.
- **Results:** the band is about 230px on desktop (from about 575px) and about 375px on a 390px phone (from about 590px). At 1280px the ingredients and instructions start on the first screen.
- The unused, unstyled `band-pattern` element was removed.

## 29. Download a recipe as one HTML file (v1.12.0)

Request: "make a button that will convert a recipe detail view to a self contained html page for download - everything needed should be inside the one html file." The draft plan went to all nine beta reviewers; the decisions below follow their input.

- **Button:** "⬇️ Download" after Fork it! and Edit recipe in the recipe band. Screen readers hear "Download as one HTML file". It's a plain link, so it works with JS off.
  - It has **no `download` attribute.** `Content-Disposition` already makes the browser save the file. With the attribute, an expired login (a redirect to /login) or the error message below would be saved as a junk `.html` file instead of being shown.
  - **Widths:** the three buttons need about 500px.
    - **Phones (≤560px):** Fork it! and Edit recipe share the first row, and Download gets its own full-width row.
    - **Wider screens:** a container query on `.band-actions` decides from the buttons' own width, not the screen's.
      - Where they get under 500px, Download is a 48px round ⬇️ button, the way Search and Random become icons on tablets; its `aria-label` still names it. That happens beside the photos on tablets and laptops, and on wide screens with many photos.
      - Elsewhere the full "⬇️ Download" label shows.
      - So the band keeps one row of buttons at every width from 561px up.
- **Route:** `GET /recipes/<rid:public_id>/download` (`recipes.download`), owner-only through `owned_recipe_or_404`. A GET with no state change, so no CSRF token; the census covers login and the 404 for other users.
- **Response headers:**
  - `text/html; charset=utf-8`, plus the app's `no-store` and `nosniff`.
  - `Content-Disposition: attachment; filename=<ascii slug>.html`, plus `filename*=UTF-8''…` when the Unicode slug differs.
  - **Slugs:**
    - NFC, lowercased, NFC again (`İ` lowercases to two characters).
    - Letters, digits and combining marks are kept, so Hindi vowel signs stay with their letters. Every other run becomes `-`.
    - At most 60 characters.
    - The ASCII fallback is made with NFKD.
    - If the slug is empty, the name is `recipe.html`.
    - Windows device names get a `recipe-` prefix: `con`, `nul`, `com1`…, and the superscript `com¹`, `lpt³`… that Windows also reserves (checked after NFKC).
  - **Its own CSP header,** equal to the file's meta policy plus `frame-ancestors 'none'`. The app's header (`style-src 'self'`, no `data:` images) would otherwise blank the styles and photos if a browser ever displayed the response instead of saving it, and two policies only ever combine more strictly.
- **The file (`templates/export.html`):** a standalone template; it doesn't extend `base.html`, so nothing new on the recipe page can leak into downloads by default.
  - **In it:** the title, the calorie stamp, tags as plain chips, ingredients with amounts, per-item calories and the total, the instructions, every photo, "Remix of <parent name>" as plain text, and a footer: "Saved from Forkful on <date>".
  - **The cover** sits in the band, larger than on the site. The other photos follow the instructions in a wrapping polaroid grid, shown whole rather than cropped square. Each photo appears once, in the recipe's order. With no photos there is no photo area (no placeholder).
  - **Print:** `@page` margins, white paper, and no shadows, tilts or tape on panels and photos. The calorie stamp keeps its look.
    - The cover sits beside the title, so the ingredients start on page one.
    - Ingredient rows, photos and the band don't split across pages; the instructions may.
    - Photos print three across.
  - **Not in it:** the nav, the username, the CSRF token, the fork dialog, Edit/Fork buttons, scripts, links, the list of remixes (other recipes' names), public ids, photo file keys, URLs, the app version, and created/updated dates.
  - The ingredient list is a macro in `_macros.html` that `show.html` shares.
- **Inlined styles:** `forkful.css` plus a small `export.css` (the export layout and print rules) in one `<style>`.
  - The Fredoka font's one `url()` becomes a base64 `data:font/woff2` URI.
  - **The build fails closed** (a 500, never a file) if:
    - that `url()` isn't found exactly once;
    - any other `url()` isn't `data:`;
    - the CSS contains `@import` or `</style`;
    - or it contains any backslash. A CSS escape such as `u\72l(` can spell a URL the other checks can't see, and our stylesheets use none.
  - It's the whole stylesheet, not a trimmed copy, so the download can't drift from the site's look. The extra ~37 KB is small next to the photos.
  - It's rebuilt per request (microseconds), so CSS edits show up without a restart.
- **The file's meta CSP**, first in `<head>` after the charset: `default-src 'none'; img-src data:; font-src data:; style-src 'sha256-…'; script-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`.
  - The hash is computed in Python over the exact CSS text the template prints, so opening the file can't make a network request or run a script, even if escaping ever failed.
  - Also in the head: `referrer: no-referrer` and `color-scheme: light`.
- **Photos:** each is re-encoded at download time to at most 1600px on the long edge (never upscaled), at JPEG quality 82. A photo that really uses transparency becomes PNG instead. No WebP, for the widest compatibility.
  - **One code path:** decoding is shared with uploads through `photos._decoding()`. It's one photo at a time under the decode lock, with JPEG draft decoding, the same pixel limit, and metadata dropped (only the ICC profile is kept).
  - **No cache.** There's nothing on disk to outlive a deleted photo.
  - **Measured:** 13 full-size 4032×3024 phone photos take about 2.1 seconds and make a file of about 4.8 MB. The demo's 13-photo pizza is 2.2 MB.
  - **Limits:** every photo is included or none.
    - **Size:** a file larger than `MAX_EXPORT_BYTES` (40 MB) stops the download.
      - The limit is checked on the finished file, styles and text included.
      - Before that, each photo's base64 size is counted before its copy is made, so a recipe far over the limit stops without doing the rest.
    - **Unreadable photos:** a photo that's missing, unreadable or over the pixel limit also stops it.
    - **Either way,** the recipe page comes back with a note instead of a partial file. A HEAD request gets the same redirect but leaves no note behind.
    - **The database:** the connection is closed once everything is read, before the photo work starts.
- **Tests (`tests/test_download.py`):**
  - Headers and filenames, including Unicode, emoji-only, path tricks, quotes, device names and long names.
  - Self-containment: every `src`/`href` and every CSS `url()` is `data:`; there's no script, link, form, iframe, `on*` or `style=`.
  - The CSP hash matches the style element, and the header matches the meta tag.
  - Content and photo order, plus downscaling, transparency and no upscaling.
  - Hostile text stays escaped.
  - No username, CSRF token, public id, file key or app URL appears.
  - The size limit and a missing photo, empty recipes, and the button on the recipe page.
  - A static check that `forkful.css` keeps exactly one `url()`.
- **Not built:** PDF, ZIP or bulk export, share links, export options, a derivative cache, an export semaphore, WebP, dark mode, or a JS save flow.
- **Kept as is:** "Saved from Forkful on <date>" is the UTC date, like every date in the app. If a previewer strips the style element, the file stays readable, but the photos show at full size.
- **Code review (all nine beta reviewers):**
  - The size limit now covers the whole file.
  - The button decides by its own width, which fixed a wrap at 1440px and up with many photos.
  - Slugs keep combining marks, and superscript device names are reserved.
  - HEAD leaves no note behind.
  - The CSS escape check was added.
  - The database connection closes early.
  - Shared code: `recipe_parts()` loads what both the page and the file show, and `photos._save_jpeg()` writes JPEGs for both uploads and copies.
  - The tests now pin:
    - the exact inlined stylesheet;
    - the meta order;
    - JPEG quality 82;
    - metadata dropped and the ICC profile kept;
    - the chosen cover;
    - what stays out (version, recipe dates, remixes);
    - corrupt and over-limit photos;
    - no cache;
    - a route-level 500 when the stylesheet fails closed;
    - the button's order and layout rules.

## 30. Notes on each ingredient (v1.13.0)

Request: "for the ingredients section, there is name, amount and calories. can you also a "notes" for each ingredient. it is a text field."

- **Schema v6** (`migrations/0006_ingredient_notes.sql`): `ingredients.notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 300)`.
  - Existing ingredients get `''`.
  - It's a plain `ALTER TABLE … ADD COLUMN`, with no table rebuild.
- **Form:** each ingredient row gets a "Notes" field (`ing-N-notes`).
  - It's one line of up to 300 characters, cleaned like names and amounts (NFC, control characters dropped, trimmed). Over 300 gives the error "Notes can be up to 300 characters."
  - A row with only a note still needs a name, and a form without notes fields (an edit page opened before the upgrade) saves with empty notes.
  - **Layout,** with named grid areas:
    - **900px and up:** one line, name | amount | calories | notes | ✕.
    - **641–899px:** notes on a second line under the other fields.
    - **Phones:** three lines: the name; then amount, calories and ✕; then notes.
  - **Placeholders:** the first row's placeholder is "Finely chopped", like the other first-row examples. Later rows say "Notes", because on narrower screens the notes line has no column heading above it.
- **Shown:** in small, muted italics under the ingredient's name. The ingredient list is the shared `ingredient_list` macro, so the recipe page and the downloaded file both show them. Ingredients without a note look as before.
- **Forks** copy each ingredient's note with it.
- **Not searchable:** notes aren't added to the word search (`recipe_text`), so its triggers and the index check don't change. Adding them later would be a migration that rebuilds the index.
- **Tests (`tests/test_notes.py`):**
  - parsing, cleaning, the limit and a note without a name;
  - the database limit and NOT NULL;
  - upgrading a populated v5 database;
  - the recipe page, edit form, re-rendering after a rejected save, and the error message and its `aria-describedby`;
  - escaping, forks and the downloaded file.
  - Tests that pinned schema version 5, or four-value ingredient rows, were updated.
