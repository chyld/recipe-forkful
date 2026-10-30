"""Public ids: the only recipe identifiers that appear in URLs and pages.

The database keeps its integer ids for joins and foreign keys; they never leave the
server. A recipe's public id is NanoID-style: 12 random characters from 0-9 and a-z
(36^12, about 62 bits). Ownership checks are the real lock, so the id only has to be
unguessable enough to hide counts and order, and the unique index retries the
vanishingly rare clash.
"""

import re
import secrets

from werkzeug.routing import BaseConverter

PUBLIC_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
PUBLIC_ID_LENGTH = 12
PUBLIC_ID_PATTERN = f"[0-9a-z]{{{PUBLIC_ID_LENGTH}}}"
PUBLIC_ID_RE = re.compile(PUBLIC_ID_PATTERN, re.ASCII)

FILE_KEY_PATTERN = "[0-9a-f]{32}"   # photo file keys (forkful/photos.py) stay 32 hex
FILE_KEY_RE = re.compile(FILE_KEY_PATTERN, re.ASCII)


def new_public_id():
    return "".join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(PUBLIC_ID_LENGTH))


class _StrictConverter(BaseConverter):
    """A URL part that must match `regex` exactly. Anything else (an old integer id,
    uppercase, the wrong length) simply doesn't match the route, so it's a 404."""

    pattern = None

    def to_url(self, value):
        # Fail loudly if code ever builds a URL from an integer id by mistake.
        if not isinstance(value, str) or not self.pattern.fullmatch(value):
            raise ValueError(f"not a valid {type(self).__name__} value: {value!r}")
        return value


class RecipeIdConverter(_StrictConverter):
    """`<rid:public_id>` in a route."""
    regex = PUBLIC_ID_PATTERN
    pattern = PUBLIC_ID_RE


class FileKeyConverter(_StrictConverter):
    """`<key:file_key>` in a route."""
    regex = FILE_KEY_PATTERN
    pattern = FILE_KEY_RE
