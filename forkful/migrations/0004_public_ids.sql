-- Forkful schema, version 4: public recipe ids.
-- URLs and pages show recipes.public_id (32 lowercase hex: 128 random bits) instead of the
-- integer id, which stays the key for every join, foreign key and the search index.
--
-- SQLite can't add a NOT NULL or CHECKed column without rebuilding the table (and recipes
-- is referenced from everywhere), so the column is added plainly and triggers enforce the
-- rules: every new recipe brings a well-formed id, and an id never changes.

BEGIN IMMEDIATE;

ALTER TABLE recipes ADD COLUMN public_id TEXT;

-- randomblob() is evaluated per row; new recipes get their ids from Python (forkful/ids.py).
UPDATE recipes SET public_id = lower(hex(randomblob(16)));

CREATE UNIQUE INDEX recipes_public_id ON recipes(public_id);

CREATE TRIGGER recipes_public_id_valid BEFORE INSERT ON recipes
WHEN NEW.public_id IS NULL
  OR typeof(NEW.public_id) <> 'text'
  OR length(NEW.public_id) <> 32
  OR length(CAST(NEW.public_id AS BLOB)) <> 32     -- text functions stop at NUL; the byte length can't
  OR NEW.public_id GLOB '*[^0-9a-f]*'
BEGIN
  SELECT RAISE(ABORT, 'recipes.public_id must be 32 lowercase hex characters');
END;

CREATE TRIGGER recipes_public_id_fixed BEFORE UPDATE OF public_id ON recipes
WHEN NEW.public_id IS NOT OLD.public_id
BEGIN
  SELECT RAISE(ABORT, 'recipes.public_id never changes');
END;

PRAGMA user_version = 4;

COMMIT;
