"""Recipe pages: list, search, create, show, edit, fork."""

from dataclasses import dataclass

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import export, forms, ids, photos, repo
from .db import close_db, get_db, transaction, utc_ts

bp = Blueprint("recipes", __name__)

CELEBRATE = "celebrate"


def owned_recipe_or_404(public_id):
    """The signed-in user's recipe with this public id. Someone else's looks exactly like none."""
    recipe = repo.get_recipe_by_public_id(get_db(), g.user["id"], public_id)
    if recipe is None:
        abort(404)
    return recipe


def uploaded_files():
    return [f for f in request.files.getlist("photos") if f and f.filename]


def apply_cover(db, user_id, recipe_id, cover, new_photo_ids, existing=()):
    """Honor the form's cover choice. Without one, the first photo stays the cover."""
    if cover is None:
        return
    kind, value = cover
    if kind == "photo":   # an existing photo, by file key (already checked to be this recipe's)
        photo_ids = {p["file_key"]: p["id"] for p in existing}
        if value in photo_ids:
            repo.set_cover(db, user_id, recipe_id, photo_ids[value])
    elif value < len(new_photo_ids):
        repo.set_cover(db, user_id, recipe_id, new_photo_ids[value])


def render_form(form, recipe=None, existing_photos=(), status=200):
    return render_template(
        "form.html", form=form, recipe=recipe, existing_photos=existing_photos,
        accept=photos.ACCEPT, calories_pattern=forms.CALORIES_PATTERN,
    ), status


def recipe_parts(db, user_id, recipe):
    """What the recipe page and its downloaded file both show, besides the recipe row itself."""
    recipe_id = recipe["id"]
    return {
        "ingredients": repo.get_ingredients(db, user_id, recipe_id),
        "tags": repo.get_tags(db, user_id, recipe_id),
        "total": repo.total_calories(db, user_id, recipe_id),
        "photos": repo.get_photos(db, user_id, recipe_id),
        "parent": repo.get_parent(db, user_id, recipe["parent_id"]),
    }


@bp.route("/")
def index():
    """All your recipes, freshest first. Searching and sorting live on /search."""
    if "tag" in request.args or "sort" in request.args:
        # Links from before v1.6 (/?tag=soup): forward them, in one hop when they're valid.
        search = read_search(request.args)
        if search.problem:
            return redirect(url_for("recipes.search", tag=request.args.getlist("tag"),
                                    sort=request.args.getlist("sort")))
        return redirect(search.url())
    db = get_db()
    user_id = g.user["id"]
    return render_template(
        "list.html",
        recipes=repo.list_recipes(db, user_id),
        card_tags=repo.tags_by_recipe(db, user_id),
    )


@dataclass(frozen=True)
class Search:
    """One /search request: words (?q=), tags (?tag=, all of them) and a sort (?sort=)."""
    text: forms.TextSearch
    tags: list
    tag_problem: str | None
    sort: str | None

    @property
    def problem(self):
        return self.tag_problem or self.text.problem

    def url(self):
        """The one canonical URL for this search: q, then sorted tags, then a non-default sort."""
        return url_for("recipes.search", q=self.text.q, tag=self.tags, sort=self.sort)

    def is_canonical(self, args):
        return (args.getlist("q") == ([self.text.q] if self.text.q else [])
                and args.getlist("tag") == self.tags
                and args.getlist("sort") == ([self.sort] if self.sort else []))


def read_search(args):
    tags, tag_problem = forms.parse_tag_search(args.getlist("tag"))
    return Search(forms.parse_text_search(args.getlist("q")), tags, tag_problem,
                  forms.parse_sort(args.getlist("sort")))


@bp.route("/search")
def search():
    """Recipes matching every word and having every picked tag, in the picked order.
    Nothing picked: every recipe."""
    search = read_search(request.args)
    if search.problem is None and not search.is_canonical(request.args):
        return redirect(search.url())   # one canonical URL per search
    db = get_db()
    user_id = g.user["id"]
    recipes = [] if search.problem else repo.list_recipes(
        db, user_id, tags=search.tags, sort=search.sort, match=search.text.match_expression())
    return render_template(
        "search.html",
        recipes=recipes,
        q=search.text.q,              # the user's own words, normalized; autoescaped wherever shown
        words=search.text.words,
        selected=search.tags,         # canonical tags only; invalid input is never echoed back
        problem=search.problem,
        tag_problem=search.tag_problem,
        sort=search.sort,
        max_search_tags=forms.MAX_SEARCH_TAGS,
        max_search_words=forms.MAX_SEARCH_WORDS,
        search_text_max=forms.SEARCH_TEXT_MAX,
        tag_counts=repo.tag_counts(db, user_id),
        card_tags=repo.tags_by_recipe(db, user_id),
    )


@bp.route("/random")
def random():
    """Jump to one of your recipes at random. ?skip=<public id> (the header sends the recipe
    you're on) avoids landing on the same one again."""
    skip = request.args.get("skip", "")
    public_id = repo.random_public_id(get_db(), g.user["id"], skip if ids.PUBLIC_ID_RE.fullmatch(skip) else None)
    if public_id is None:
        flash("No recipes to pick from yet. Create one first!", "info")
        return redirect(url_for("recipes.index"))
    return redirect(url_for("recipes.show", public_id=public_id))


@bp.route("/recipes/new", methods=["GET", "POST"])
def new():
    if request.method == "GET":
        return render_form(forms.blank_form())

    form = forms.parse_recipe_form(request.form)
    if form.has_errors:
        return render_form(form.with_blank_rows(forms.NEW_BLANK_ROWS), status=400)

    try:
        stored = photos.store_uploads(uploaded_files())
    except photos.PhotoError as exc:
        form.errors["photos"] = str(exc)
        return render_form(form.with_blank_rows(forms.NEW_BLANK_ROWS), status=400)

    db = get_db()
    user_id = g.user["id"]
    try:
        with transaction(db):
            ts = utc_ts()
            recipe_id, public_id = repo.insert_recipe(db, user_id, form.name, form.instructions, ts)
            repo.replace_ingredients(db, user_id, recipe_id, form.ingredients())
            repo.replace_tags(db, user_id, recipe_id, form.tags)
            new_ids = repo.add_photos(db, user_id, recipe_id, stored, ts)
            apply_cover(db, user_id, recipe_id, form.cover, new_ids)
    except BaseException:
        photos.unlink_files([p.path for p in stored])
        raise

    flash("Recipe created — looking delicious!", CELEBRATE)
    return redirect(url_for("recipes.show", public_id=public_id))


@bp.route("/recipes/<rid:public_id>")
def show(public_id):
    db = get_db()
    user_id = g.user["id"]
    recipe = owned_recipe_or_404(public_id)
    return render_template(
        "show.html",
        recipe=recipe,
        **recipe_parts(db, user_id, recipe),
        forks=repo.get_forks(db, user_id, recipe["id"]),
        fork_suggestion=forms.fork_name_suggestion(recipe["name"]),
    )


@bp.route("/recipes/<rid:public_id>/download")
def download(public_id):
    """The recipe as one self-contained HTML file (PLAN.md §29). Every photo goes in, or nothing does."""
    recipe = owned_recipe_or_404(public_id)
    parts = recipe_parts(get_db(), g.user["id"], recipe)
    close_db()                  # everything is read; don't hold the connection through the photo work
    try:
        page, csp = export.render(recipe, **parts)
    except export.ExportError as exc:
        if request.method == "GET":                   # a HEAD must not leave a note for the next page
            flash(str(exc), "info")
        return redirect(url_for("recipes.show", public_id=public_id))
    response = current_app.response_class(page, mimetype="text/html")
    response.headers.set("Content-Disposition", "attachment", **export.disposition(recipe["name"]))
    # The app's own CSP would block the inline style and data: photos if a browser ever showed
    # this response instead of saving it; give it the file's policy (headers only ever add up).
    response.headers["Content-Security-Policy"] = csp + "; frame-ancestors 'none'"
    return response


@bp.route("/recipes/<rid:public_id>/edit", methods=["GET", "POST"])
def edit(public_id):
    db = get_db()
    user_id = g.user["id"]
    recipe = owned_recipe_or_404(public_id)
    recipe_id = recipe["id"]
    existing = repo.get_photos(db, user_id, recipe_id)

    if request.method == "GET":
        form = forms.form_from_recipe(recipe, repo.get_ingredients(db, user_id, recipe_id),
                                      repo.get_tags(db, user_id, recipe_id))
        return render_form(form, recipe, existing)

    form = forms.parse_recipe_form(request.form, photo_keys=[p["file_key"] for p in existing])
    if form.has_errors:
        return render_form(form.with_blank_rows(forms.EDIT_BLANK_ROWS), recipe, existing, status=400)

    try:
        stored = photos.store_uploads(uploaded_files())
    except photos.PhotoError as exc:
        form.errors["photos"] = str(exc)
        return render_form(form.with_blank_rows(forms.EDIT_BLANK_ROWS), recipe, existing, status=400)

    try:
        with transaction(db):
            ts = utc_ts()
            if not repo.update_recipe(db, user_id, recipe_id, form.name, form.instructions, ts):
                abort(404)
            repo.replace_ingredients(db, user_id, recipe_id, form.ingredients())
            repo.replace_tags(db, user_id, recipe_id, form.tags)
            new_ids = repo.add_photos(db, user_id, recipe_id, stored, ts)
            apply_cover(db, user_id, recipe_id, form.cover, new_ids, existing)
    except BaseException:
        photos.unlink_files([p.path for p in stored])
        raise

    flash("Saved!", "success")
    return redirect(url_for("recipes.show", public_id=public_id))


@bp.route("/recipes/<rid:public_id>/photos/<key:file_key>/delete", methods=["POST"])
def delete_photo(public_id, file_key):
    """Delete one photo right away. The edit page calls this with fetch (204) or as a plain form button."""
    recipe_id = owned_recipe_or_404(public_id)["id"]
    db = get_db()
    with transaction(db):
        deleted = repo.delete_photo(db, g.user["id"], recipe_id, file_key)   # only this recipe's photo
        if deleted is None:
            abort(404)
        repo.touch_recipe(db, g.user["id"], recipe_id, utc_ts())
    photos.unlink_photo_rows([deleted])   # only after the commit succeeded
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    flash("Photo deleted.", "success")
    return redirect(url_for("recipes.edit", public_id=public_id))


@bp.route("/recipes/<rid:public_id>/fork", methods=["GET", "POST"])
def fork(public_id):
    recipe = owned_recipe_or_404(public_id)

    if request.method == "GET":
        return render_template("fork.html", recipe=recipe,
                               name=forms.fork_name_suggestion(recipe["name"]), error=None)

    name, error = forms.parse_fork_name(request.form.get("name", ""), recipe["name"])
    if error:
        return render_template("fork.html", recipe=recipe, name=name, error=error), 400

    db = get_db()
    with transaction(db):   # BEGIN IMMEDIATE before reading the source: the copy is one snapshot
        source = repo.get_recipe(db, g.user["id"], recipe["id"])
        if source is None:
            abort(404)
        name, error = forms.parse_fork_name(name, source["name"])
        if error is None:
            new_public_id = repo.fork_recipe(db, g.user["id"], source, name, utc_ts())
    if error:
        return render_template("fork.html", recipe=source, name=name, error=error), 400

    flash("Remix created — make it yours!", CELEBRATE)
    return redirect(url_for("recipes.show", public_id=new_public_id))
