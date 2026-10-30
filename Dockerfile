# Forkful: Flask + SQLite + Waitress, served over plain HTTP on port 7777.
FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLASK_APP=forkful:create_app \
    FORKFUL_DATA_DIR=/data \
    TMPDIR=/data/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --require-hashes --only-binary=:all: -r requirements.txt

# One non-root user owns /data, so the server and `docker compose exec` write files as the same uid.
# A fresh named volume copies this ownership on first mount.
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /app app \
 && mkdir -p /data/photos /data/tmp \
 && chown -R app:app /data

COPY --chown=app:app forkful ./forkful
# Stamp the build time; the footer shows it next to the version. Any code change re-runs this.
RUN date -u +"%Y-%m-%d %H:%M UTC" > forkful/BUILD
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

USER app
EXPOSE 7777
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["server"]
