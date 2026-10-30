#!/bin/sh
# Default: migrate, then serve. Anything else is passed through, e.g.
#   docker compose run --rm web flask create-user alice
set -eu

case "${1:-server}" in
  server)
    mkdir -p "$FORKFUL_DATA_DIR/photos" "$TMPDIR"
    flask migrate
    # Waitress's body limit is 1 MiB above Flask's MAX_CONTENT_LENGTH (200 MiB) so the
    # app's friendly 413 page wins. The module path must stay last, after --call.
    exec waitress-serve \
      --host=0.0.0.0 --port=7777 \
      --threads=4 \
      --connection-limit=100 \
      --channel-timeout=60 \
      --max-request-body-size=210763776 \
      --call forkful:create_app
    ;;
  flask)
    shift
    exec flask "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
