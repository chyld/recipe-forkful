-- Forkful schema, version 6: a free-text note on each ingredient ("finely chopped", "or use shallots").
-- One line, up to 300 characters, '' when there's none. Existing ingredients get ''.
-- Notes aren't part of the word search (recipe_text), so its triggers don't change.

BEGIN IMMEDIATE;

ALTER TABLE ingredients ADD COLUMN notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 300);

PRAGMA user_version = 6;

COMMIT;
