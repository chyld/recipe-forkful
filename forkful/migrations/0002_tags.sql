-- Forkful schema, version 2: recipe tags.
-- A tag is 2-24 letters a-z. Tags live per recipe (no shared tag dictionary), carry
-- their owner, and the composite key means a tag can only sit on its owner's recipe.

BEGIN IMMEDIATE;

CREATE TABLE recipe_tags (
  recipe_id INTEGER NOT NULL,
  user_id   INTEGER NOT NULL,
  tag       TEXT NOT NULL CHECK (
              typeof(tag) = 'text'
              AND length(tag) BETWEEN 2 AND 24
              AND tag NOT GLOB '*[^a-z]*'
              -- text functions stop at NUL; the byte length can't, so "ab\0!" is rejected
              AND length(CAST(tag AS BLOB)) = length(tag)
            ),
  position  INTEGER NOT NULL CHECK (position BETWEEN 0 AND 9),   -- typed order; at most 10 tags
  PRIMARY KEY (recipe_id, tag),
  UNIQUE (recipe_id, position),
  FOREIGN KEY (recipe_id, user_id) REFERENCES recipes(id, user_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX recipe_tags_user_tag ON recipe_tags(user_id, tag, recipe_id);

PRAGMA user_version = 2;

COMMIT;
