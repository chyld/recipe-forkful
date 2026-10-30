-- Forkful schema, version 3: full-text search over recipe names, ingredient names and
-- instructions (not amounts, not tags).
--
-- One row per recipe, rowid = recipe id. There is deliberately no user_id here: searches
-- join back to recipes and filter on recipes.user_id, the one source of ownership.
--
-- Triggers keep the index in step with every write, whatever code makes it (create,
-- edit, fork, a future delete, or raw SQL). Each trigger rebuilds its recipe's row from
-- the base tables with the same statement, so the index can't drift from them. A rebuild
-- for a recipe that no longer exists inserts nothing, so cascaded deletes can't bring a
-- deleted recipe back.

BEGIN IMMEDIATE;

CREATE VIRTUAL TABLE recipe_text USING fts5(
  name, ingredients, instructions,
  tokenize = 'porter unicode61 remove_diacritics 2'   -- "tomatoes" finds "tomato", "creme" finds "crème"
);

CREATE TRIGGER recipe_text_recipe_insert AFTER INSERT ON recipes BEGIN
  INSERT INTO recipe_text (rowid, name, ingredients, instructions)
    SELECT r.id, r.name,
           (SELECT COALESCE(group_concat(i.name, ' '), '') FROM ingredients i WHERE i.recipe_id = r.id),
           r.instructions
    FROM recipes r WHERE r.id = NEW.id;
END;

-- Only text changes reindex: touching updated_at (a photo upload) doesn't.
CREATE TRIGGER recipe_text_recipe_update AFTER UPDATE OF id, name, instructions ON recipes BEGIN
  DELETE FROM recipe_text WHERE rowid IN (OLD.id, NEW.id);
  INSERT INTO recipe_text (rowid, name, ingredients, instructions)
    SELECT r.id, r.name,
           (SELECT COALESCE(group_concat(i.name, ' '), '') FROM ingredients i WHERE i.recipe_id = r.id),
           r.instructions
    FROM recipes r WHERE r.id = NEW.id;
END;

CREATE TRIGGER recipe_text_recipe_delete AFTER DELETE ON recipes BEGIN
  DELETE FROM recipe_text WHERE rowid = OLD.id;
END;

CREATE TRIGGER recipe_text_ingredient_insert AFTER INSERT ON ingredients BEGIN
  DELETE FROM recipe_text WHERE rowid = NEW.recipe_id;
  INSERT INTO recipe_text (rowid, name, ingredients, instructions)
    SELECT r.id, r.name,
           (SELECT COALESCE(group_concat(i.name, ' '), '') FROM ingredients i WHERE i.recipe_id = r.id),
           r.instructions
    FROM recipes r WHERE r.id = NEW.recipe_id;
END;

CREATE TRIGGER recipe_text_ingredient_update AFTER UPDATE OF recipe_id, name ON ingredients BEGIN
  DELETE FROM recipe_text WHERE rowid IN (OLD.recipe_id, NEW.recipe_id);
  INSERT INTO recipe_text (rowid, name, ingredients, instructions)
    SELECT r.id, r.name,
           (SELECT COALESCE(group_concat(i.name, ' '), '') FROM ingredients i WHERE i.recipe_id = r.id),
           r.instructions
    FROM recipes r WHERE r.id IN (OLD.recipe_id, NEW.recipe_id);
END;

CREATE TRIGGER recipe_text_ingredient_delete AFTER DELETE ON ingredients BEGIN
  DELETE FROM recipe_text WHERE rowid = OLD.recipe_id;
  INSERT INTO recipe_text (rowid, name, ingredients, instructions)
    SELECT r.id, r.name,
           (SELECT COALESCE(group_concat(i.name, ' '), '') FROM ingredients i WHERE i.recipe_id = r.id),
           r.instructions
    FROM recipes r WHERE r.id = OLD.recipe_id;
END;

-- Back-fill every existing recipe.
INSERT INTO recipe_text (rowid, name, ingredients, instructions)
  SELECT r.id, r.name,
         (SELECT COALESCE(group_concat(i.name, ' '), '') FROM ingredients i WHERE i.recipe_id = r.id),
         r.instructions
  FROM recipes r;

PRAGMA user_version = 3;

COMMIT;
