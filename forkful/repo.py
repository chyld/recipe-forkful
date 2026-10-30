"""All recipe SQL. Every function takes user_id; nothing can fetch or change a recipe by id alone.

`recipe_id` is always the internal integer id. Pages and URLs only ever see `public_id`.
"""

import sqlite3

from . import ids

RECIPE_COLUMNS = "id, public_id, user_id, parent_id, name, instructions, created_at, updated_at"
PUBLIC_ID_RETRIES = 3


# The only ORDER BY text a sort can pick; a request value never reaches the SQL.
# Ties always fall back to freshest first, so every order is stable.
ORDER_BY = {
    None: "r.updated_at DESC, r.id DESC",
    "cal-asc": "total_calories ASC, r.updated_at DESC, r.id DESC",
    "cal-desc": "total_calories DESC, r.updated_at DESC, r.id DESC",
}


def list_recipes(db, user_id, tags=(), sort=None, match=None):
    """This user's recipes, freshest first or in `sort` order (a key of ORDER_BY).

    With `tags`, only recipes that have every one of them. With `match` (an FTS5 expression
    from forms.TextSearch, always bound, never built from raw input), only recipes whose
    name, ingredients or instructions match it.
    """
    order_by = ORDER_BY[sort]
    params = {"user": user_id}
    tag_filter = text_filter = ""
    if tags:
        names = [f":t{i}" for i in range(len(tags))]           # bound placeholders only, never values
        params.update({f"t{i}": tag for i, tag in enumerate(tags)})
        params["n"] = len(tags)
        tag_filter = f"""
          AND r.id IN (SELECT recipe_id FROM recipe_tags
                       WHERE user_id = :user AND tag IN ({", ".join(names)})
                       GROUP BY recipe_id HAVING COUNT(*) = :n)"""
    if match:
        # The index holds everyone's text; ownership comes from r.user_id, as for every query.
        params["match"] = match
        text_filter = """
          AND r.id IN (SELECT rowid FROM recipe_text WHERE recipe_text MATCH :match)"""
    return db.execute(
        """
        SELECT r.id, r.public_id, r.name, r.parent_id, r.updated_at,
               p.name AS parent_name,
               (SELECT COALESCE(SUM(i.calories), 0) FROM ingredients i WHERE i.recipe_id = r.id)
                   AS total_calories,
               (SELECT ph.file_key FROM photos ph WHERE ph.recipe_id = r.id
                ORDER BY ph.position, ph.id LIMIT 1) AS cover_key
        FROM recipes r
        LEFT JOIN recipes p ON p.id = r.parent_id AND p.user_id = r.user_id
        WHERE r.user_id = :user""" + tag_filter + text_filter + """
        ORDER BY """ + order_by,
        params,
    ).fetchall()


# --- tags ----------------------------------------------------------------------------

def get_tags(db, user_id, recipe_id):
    """One recipe's tags, in the order they were typed."""
    return [row[0] for row in db.execute(
        "SELECT tag FROM recipe_tags WHERE recipe_id = ? AND user_id = ? ORDER BY position",
        (recipe_id, user_id),
    )]


def tags_by_recipe(db, user_id):
    """Every tag this user has, grouped by recipe id, in one query (for the list page)."""
    grouped = {}
    for recipe_id, tag in db.execute(
        "SELECT recipe_id, tag FROM recipe_tags WHERE user_id = ? ORDER BY recipe_id, position", (user_id,)
    ):
        grouped.setdefault(recipe_id, []).append(tag)
    return grouped


def tag_counts(db, user_id):
    """[(tag, number of this user's recipes with it)], alphabetical. Never counts anyone else's."""
    return db.execute(
        "SELECT tag, COUNT(*) AS n FROM recipe_tags WHERE user_id = ? GROUP BY tag ORDER BY tag", (user_id,)
    ).fetchall()


def replace_tags(db, user_id, recipe_id, tags):
    """Replace a recipe's tags with `tags` (already validated), keeping their order."""
    db.execute("DELETE FROM recipe_tags WHERE recipe_id = ? AND user_id = ?", (recipe_id, user_id))
    db.executemany(
        "INSERT INTO recipe_tags (recipe_id, user_id, tag, position) "
        "SELECT id, user_id, ?, ? FROM recipes WHERE id = ? AND user_id = ?",
        [(tag, position, recipe_id, user_id) for position, tag in enumerate(tags)],
    )


def get_recipe(db, user_id, recipe_id):
    return db.execute(
        f"SELECT {RECIPE_COLUMNS} FROM recipes WHERE id = ? AND user_id = ?",
        (recipe_id, user_id),
    ).fetchone()


def get_recipe_by_public_id(db, user_id, public_id):
    """One lookup on both columns, so "not yours" and "doesn't exist" are the same miss."""
    return db.execute(
        f"SELECT {RECIPE_COLUMNS} FROM recipes WHERE public_id = ? AND user_id = ?",
        (public_id, user_id),
    ).fetchone()


def get_ingredients(db, user_id, recipe_id):
    return db.execute(
        """
        SELECT i.position, i.name, i.amount, i.calories
        FROM ingredients i JOIN recipes r ON r.id = i.recipe_id
        WHERE i.recipe_id = ? AND r.user_id = ?
        ORDER BY i.position
        """,
        (recipe_id, user_id),
    ).fetchall()


def get_photos(db, user_id, recipe_id):
    return db.execute(
        """
        SELECT ph.id, ph.file_key, ph.content_type, ph.width, ph.height, ph.position
        FROM photos ph JOIN recipes r ON r.id = ph.recipe_id
        WHERE ph.recipe_id = ? AND r.user_id = ?
        ORDER BY ph.position, ph.id
        """,
        (recipe_id, user_id),
    ).fetchall()


def get_photo_by_key(db, user_id, file_key):
    return db.execute(
        """
        SELECT ph.file_key, ph.content_type
        FROM photos ph JOIN recipes r ON r.id = ph.recipe_id
        WHERE ph.file_key = ? AND r.user_id = ?
        """,
        (file_key, user_id),
    ).fetchone()


def total_calories(db, user_id, recipe_id):
    return db.execute(
        """
        SELECT COALESCE(SUM(i.calories), 0)
        FROM ingredients i JOIN recipes r ON r.id = i.recipe_id
        WHERE i.recipe_id = ? AND r.user_id = ?
        """,
        (recipe_id, user_id),
    ).fetchone()[0]


def random_public_id(db, user_id, skip=None):
    """The public id of one of this user's recipes, picked at random, or None if they have none.
    `skip` (the recipe being viewed) sorts last, so it's only picked when it's the only one."""
    row = db.execute(
        "SELECT public_id FROM recipes WHERE user_id = ? ORDER BY public_id IS ?, random() LIMIT 1",
        (user_id, skip),
    ).fetchone()
    return row[0] if row else None


def get_parent(db, user_id, parent_id):
    if parent_id is None:
        return None
    return db.execute(
        "SELECT id, public_id, name FROM recipes WHERE id = ? AND user_id = ?", (parent_id, user_id)
    ).fetchone()


def get_forks(db, user_id, recipe_id):
    return db.execute(
        "SELECT id, public_id, name FROM recipes WHERE parent_id = ? AND user_id = ? ORDER BY created_at, id",
        (recipe_id, user_id),
    ).fetchall()


# --- writes (callers wrap these in one transaction) ---------------------------

def insert_recipe(db, user_id, name, instructions, ts, parent_id=None):
    """Insert a recipe with a fresh random public id. Returns (id, public_id)."""
    for attempt in range(1, PUBLIC_ID_RETRIES + 1):
        public_id = ids.new_public_id()
        try:
            cur = db.execute(
                "INSERT INTO recipes (public_id, user_id, parent_id, name, instructions, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (public_id, user_id, parent_id, name, instructions, ts, ts),
            )
        except sqlite3.IntegrityError as exc:
            # 128 random bits won't collide in practice, but the unique index is the judge.
            # A failed INSERT only undoes itself, so the transaction carries on.
            if "recipes.public_id" in str(exc) and attempt < PUBLIC_ID_RETRIES:
                continue
            raise
        return cur.lastrowid, public_id


def update_recipe(db, user_id, recipe_id, name, instructions, ts):
    """Overwrite in place. Returns False if the recipe isn't this user's."""
    cur = db.execute(
        "UPDATE recipes SET name = ?, instructions = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (name, instructions, ts, recipe_id, user_id),
    )
    return cur.rowcount == 1


def replace_ingredients(db, user_id, recipe_id, ingredients):
    """Replace every ingredient row. `ingredients` is [(position, name, amount, calories)]."""
    owned = "SELECT id FROM recipes WHERE id = ? AND user_id = ?"
    db.execute(f"DELETE FROM ingredients WHERE recipe_id IN ({owned})", (recipe_id, user_id))
    db.executemany(
        f"INSERT INTO ingredients (recipe_id, position, name, amount, calories) "
        f"SELECT id, ?, ?, ?, ? FROM recipes WHERE id = ? AND user_id = ?",
        [(pos, name, amount, cal, recipe_id, user_id) for pos, name, amount, cal in ingredients],
    )


def delete_photo(db, user_id, recipe_id, file_key):
    """Delete one photo of this user's recipe. Returns (file_key, content_type), or None if not found."""
    row = db.execute(
        """
        DELETE FROM photos
        WHERE file_key = ? AND recipe_id IN (SELECT id FROM recipes WHERE id = ? AND user_id = ?)
        RETURNING file_key, content_type
        """,
        (file_key, recipe_id, user_id),
    ).fetchone()
    return (row["file_key"], row["content_type"]) if row else None


def touch_recipe(db, user_id, recipe_id, ts):
    db.execute("UPDATE recipes SET updated_at = ? WHERE id = ? AND user_id = ?", (ts, recipe_id, user_id))


def add_photos(db, user_id, recipe_id, photos, ts):
    """Append photo rows after the current last position. Returns the new photo ids, in order.

    `photos` are photos.StoredPhoto. The cover is the photo with the lowest position, so on a
    recipe with no photos yet the first upload becomes the cover.
    """
    next_position = db.execute(
        """
        SELECT COALESCE(MAX(ph.position) + 1, 0)
        FROM photos ph JOIN recipes r ON r.id = ph.recipe_id
        WHERE ph.recipe_id = ? AND r.user_id = ?
        """,
        (recipe_id, user_id),
    ).fetchone()[0]
    new_ids = []
    for offset, photo in enumerate(photos):
        cur = db.execute(
            """
            INSERT INTO photos (recipe_id, file_key, content_type, width, height, byte_size, position, created_at)
            SELECT id, ?, ?, ?, ?, ?, ?, ? FROM recipes WHERE id = ? AND user_id = ?
            """,
            (photo.file_key, photo.content_type, photo.width, photo.height, photo.byte_size,
             next_position + offset, ts, recipe_id, user_id),
        )
        if cur.rowcount != 1:
            raise LookupError("recipe vanished while adding photos")
        new_ids.append(cur.lastrowid)
    return new_ids


def set_cover(db, user_id, recipe_id, photo_id):
    """Make a photo of this user's recipe its cover by moving it ahead of the others."""
    db.execute(
        """
        UPDATE photos
        SET position = (SELECT MIN(position) FROM photos WHERE recipe_id = :recipe) - 1
        WHERE id = :photo AND recipe_id = :recipe
          AND recipe_id IN (SELECT id FROM recipes WHERE id = :recipe AND user_id = :user)
          AND position > (SELECT MIN(position) FROM photos WHERE recipe_id = :recipe)
        """,
        {"photo": photo_id, "recipe": recipe_id, "user": user_id},
    )


def fork_recipe(db, user_id, source, name, ts):
    """Copy instructions, ingredients and tags (never photos) into a new child recipe.

    `source` is the parent row, already loaded with get_recipe(db, user_id, ...) inside
    the same transaction, so the copy is one snapshot. Returns the new recipe's public id.
    """
    new_id, public_id = insert_recipe(db, user_id, name, source["instructions"], ts, parent_id=source["id"])
    db.execute(
        "INSERT INTO ingredients (recipe_id, position, name, amount, calories) "
        "SELECT ?, position, name, amount, calories FROM ingredients WHERE recipe_id = ? ORDER BY position",
        (new_id, source["id"]),
    )
    db.execute(
        "INSERT INTO recipe_tags (recipe_id, user_id, tag, position) "
        "SELECT ?, user_id, tag, position FROM recipe_tags WHERE recipe_id = ? AND user_id = ?",
        (new_id, source["id"], user_id),
    )
    return public_id
