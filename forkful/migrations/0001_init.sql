-- Forkful schema, version 1.
-- Applied by `flask migrate` with executescript under autocommit, so this file
-- owns its transaction and bumps user_version inside it.

BEGIN IMMEDIATE;

CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- ASCII-only by validation, so NOCASE is exact
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,                          -- sha256 hex of the token in the cookie
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
  -- A parent must belong to the same user; the database enforces it.
  FOREIGN KEY (parent_id, user_id) REFERENCES recipes(id, user_id)
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
  -- AUTOINCREMENT: ids are never reused, so a stale "remove photo 7" from another
  -- tab can't delete a newer photo that would otherwise have inherited id 7.
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  recipe_id    INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  file_key     TEXT NOT NULL UNIQUE
               CHECK (length(file_key) = 32 AND file_key NOT GLOB '*[^0-9a-f]*'),
  content_type TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
  width        INTEGER NOT NULL,
  height       INTEGER NOT NULL,
  byte_size    INTEGER NOT NULL,
  position     INTEGER NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX photos_recipe ON photos(recipe_id, position);

PRAGMA user_version = 1;

COMMIT;
