-- Forkful schema, version 5: shorter public recipe ids.
-- v4's 32-hex ids become NanoID-style ids: 12 characters from 0-9 and a-z (about 62 bits).
-- Every recipe gets a new id once, here; after that ids never change again.
--
-- The v4 triggers forbid changing an id and only accept 32 hex, so they're replaced.
-- random() is evaluated per call; (random() % 36 + 36) % 36 is 0..35 without abs(),
-- which would overflow on the one value of random() that has no positive twin.

BEGIN IMMEDIATE;

DROP TRIGGER recipes_public_id_valid;
DROP TRIGGER recipes_public_id_fixed;

UPDATE recipes SET public_id =
     substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1)
  || substr('0123456789abcdefghijklmnopqrstuvwxyz', (random() % 36 + 36) % 36 + 1, 1);

CREATE TRIGGER recipes_public_id_valid BEFORE INSERT ON recipes
WHEN NEW.public_id IS NULL
  OR typeof(NEW.public_id) <> 'text'
  OR length(NEW.public_id) <> 12
  OR length(CAST(NEW.public_id AS BLOB)) <> 12     -- text functions stop at NUL; the byte length can't
  OR NEW.public_id GLOB '*[^0-9a-z]*'
BEGIN
  SELECT RAISE(ABORT, 'recipes.public_id must be 12 characters from 0-9 and a-z');
END;

CREATE TRIGGER recipes_public_id_fixed BEFORE UPDATE OF public_id ON recipes
WHEN NEW.public_id IS NOT OLD.public_id
BEGIN
  SELECT RAISE(ABORT, 'recipes.public_id never changes');
END;

PRAGMA user_version = 5;

COMMIT;
