# 🍴 Forkful

*Cook it. Fork it. Love it.*

Forkful is a private recipe box for a few people. Each person sees only their own recipes.
- Write recipes with ingredients (each with an amount, calories and an optional note like "finely chopped"), instructions and photos. Total calories are added up for you, and you pick which photo is the cover.
- **Fork** any recipe into a remix. Each recipe shows its parent and its forks.
- **Tag** recipes, then **search** by tags and by words in names, ingredients and instructions. Sort by calories.
- **🎲 Random** jumps to one of your recipes.
- **⬇️ Download** saves a recipe as one self-contained HTML file, photos included. It opens offline and prints well.

It runs with Docker Compose, uses SQLite, and serves plain HTTP on port **7777**. The design is in [PLAN.md](PLAN.md).

> ⚠️ **Plain HTTP on the internet.** Forkful does not use TLS. Anyone who can watch the network can read passwords and steal logins. That includes public Wi-Fi, ISPs, and any network along the way.
> - Never reuse a password from another site.
> - Use `flask set-password` to change a password. It also logs that person out everywhere.
> - To keep it off the network entirely, see [Who can reach it](#who-can-reach-it).

## Requirements

- **Docker with Compose v2.** The command is `docker compose`, not the old `docker-compose`.
  - On Linux, install Docker Engine and the Compose plugin.
  - On macOS and Windows, install Docker Desktop.
- **git**, to download the code.
- **A 64-bit machine:** Intel/AMD (x86-64) or ARM64 (Apple Silicon Macs, Raspberry Pi 4/5 running a 64-bit OS, ARM cloud servers). A 32-bit Raspberry Pi OS won't build.
- **Up to 2 GB of memory** for the container (its limit), plus disk space for photos.
- **Port 7777** free, unless you change it (see [Port](#port)).

## Install

1. **Download the code:**

   ```sh
   git clone https://github.com/chyld/recipe-forkful.git
   cd recipe-forkful
   ```

   On Windows, clone with `git clone --config core.autocrlf=input https://github.com/chyld/recipe-forkful.git` instead. Otherwise git may give `entrypoint.sh` Windows line endings, and the container won't start (see [Troubleshooting](#troubleshooting)).

2. **Build and start it:**

   ```sh
   docker compose up -d --build
   ```

   The first build downloads Python and the app's packages, so it takes a few minutes. Later builds are quick. On every start, the database is created or upgraded automatically.

3. **Check that it's running:**

   ```sh
   docker compose ps
   ```

   Within about half a minute, the status should read **`(healthy)`**. If it doesn't, run `docker compose logs web`.

4. **Create your account.** There is no sign-up page. Replace `alice` with the username you want; you'll be asked for the password twice:

   ```sh
   docker compose exec web flask create-user alice
   ```

5. **Open it.** Go to **http://localhost:7777** on the same machine, or **http://<the-server's-address>:7777** from another device, and log in.

Add an account for each person the same way (see [Users](#users)).

## Configuration

Settings live in `compose.yaml`. After changing them, run `docker compose up -d` (add `--build` if you changed code).

### Port

`compose.yaml` maps the host's port 7777 to the app. The left number is the port you open in the browser. For example, to use 8080:

```yaml
    ports:
      - "8080:7777"
```

### Who can reach it

By default Forkful listens on every network interface, so other devices can reach it. If the machine has a public address, that includes the whole internet. To allow only the machine itself:

```yaml
    ports:
      - "127.0.0.1:7777:7777"
```

Otherwise, use a firewall to choose who can reach port 7777.

### Secret key

On first start, Forkful creates a random secret key in its data volume and keeps it. It signs login cookies, so you don't need to do anything. To supply your own instead (at least 32 characters), add it to `compose.yaml`:

```yaml
services:
  web:
    environment:
      SECRET_KEY: "a long random string of at least 32 characters"
```

Changing the key logs everyone out.

### Limits

These are set in `create_app()` in `forkful/__init__.py`. Changing them needs `docker compose up -d --build`.

| Setting | Default |
|---|---|
| Size of one photo (`MAX_PHOTO_BYTES`) | 30 MB |
| Size of one save, photos included (`MAX_CONTENT_LENGTH`) | 200 MB |
| Pixels in one photo (`MAX_PHOTO_PIXELS`) | 60 megapixels |
| Size of a downloaded recipe file (`MAX_EXPORT_BYTES`) | 40 MB |
| How long a login lasts (`PERMANENT_SESSION_LIFETIME`) | 30 days |
| Failed logins before a 15-minute pause (`THROTTLE_PAIR_LIMIT`, `THROTTLE_IP_LIMIT`) | 10 per username per IP, 50 per IP |

The container's memory limit (`mem_limit: 2g`) and restart policy (`restart: unless-stopped`, so it comes back after a reboot) are in `compose.yaml`.

## Start and stop

```sh
docker compose up -d --build      # build and start on http://<host>:7777
docker compose logs -f web        # watch the logs
docker compose stop               # stop (data is kept)
docker compose start              # start again
```

## Where your data lives

Everything is in one Docker volume, **`forkful-data`**, mounted at `/data` in the container:
- `forkful.db`: the SQLite database (users, recipes, ingredients, tags and photo records).
- `photos/`: one file per photo. Uploads are re-encoded on the way in: location data and other metadata are removed, and the photo is turned upright and capped at 4096px.
- `secret_key`: the key that signs login cookies.

`docker compose down` keeps the volume, and so do rebuilds and upgrades.

> 🔥 **`docker compose down -v` and `docker volume rm forkful-data` delete every recipe and photo, and can't be undone.**

## Users

There is no sign-up page. Accounts are created from the command line:

```sh
docker compose exec web flask create-user alice     # asks for the password twice
docker compose exec web flask set-password alice    # new password; logs alice out everywhere
```

- Usernames are 1–32 characters: letters, digits, `.`, `-` and `_`. They ignore case.
- Passwords need at least 5 characters.
- Forgotten password: set a new one with `set-password`.

For scripts, which have no terminal for the prompt, pipe the password in:

```sh
printf '%s\n' "$PASSWORD" | docker compose exec -T web flask create-user alice --password-stdin
```

If the container isn't running, use `docker compose run --rm web flask create-user alice`.

## Backup and restore

These commands need a Unix shell: Linux, macOS, or WSL or Git Bash on Windows.

Stop the app so the database and photos are copied at the same moment. The archive is written under a temporary name and renamed only after it's complete, so an interrupted backup can't pass for a good one.

```sh
docker compose stop
docker compose run --rm -T --no-deps --entrypoint tar web czf - -C /data --exclude=./tmp . > forkful-backup.partial \
  && mv forkful-backup.partial forkful-$(date +%F).tgz
docker compose start
```

Keep backups somewhere other than the server, and off `/tmp`, which many systems clear on reboot.

To restore, the archive is checked first. `/data` is only wiped if the check passes:

```sh
docker compose stop
docker compose run --rm -T --no-deps --entrypoint sh web -c \
  'cat > /tmp/restore.tgz && tar tzf /tmp/restore.tgz > /dev/null && find /data -mindepth 1 -delete && tar xzf /tmp/restore.tgz -C /data' \
  < forkful-YYYY-MM-DD.tgz
docker compose start
```

The same two steps move Forkful to a new machine. Back up on the old one, then [install](#install) on the new one and restore the archive there before creating any accounts.

## Upgrade

1. Back up first (see above).
2. Get the new code and rebuild:

   ```sh
   git pull
   docker compose up -d --build
   ```

   Database migrations run automatically at startup.
3. Check the result with `docker compose logs --tail=50 web`. The footer of every page shows the version and build time.

If an upgrade fails:
1. Run `docker compose down`. The volume is kept.
2. Put the previous version of the code back (`git checkout <previous commit>`) and run `docker compose up -d --build`.
3. Restore the backup if the new version had already changed the database.

## Uninstall

```sh
docker compose down                 # stop and remove the container (the volume is kept)
docker volume rm forkful-data       # 🔥 deletes every recipe and photo; back up first
docker image rm forkful-web         # remove the built image
```

Then delete the `recipe-forkful` folder.

## Troubleshooting

- **The browser can't connect.**
  - Run `docker compose ps` and `docker compose logs web`.
  - From another device, use the server's address, not `localhost`.
  - Check the firewall, and the `ports:` line in `compose.yaml`.
- **"port is already allocated"** when starting. Something else is using port 7777. Pick another port (see [Port](#port)).
- **The container keeps restarting, and the logs say `exec /usr/local/bin/entrypoint.sh: no such file or directory`.** Git on Windows gave `entrypoint.sh` Windows line endings. Fix it, then rebuild (this throws away any edits you made to the code):

  ```sh
  git config core.autocrlf input
  git rm --cached -r -q . && git reset --hard
  docker compose up -d --build
  ```

- **The build fails with "No matching distribution found".** The machine is probably 32-bit. Forkful needs a 64-bit OS (see [Requirements](#requirements)).
- **The logs say "unable to open database file" or "readonly database".** The volume is owned by root, which can happen if it was created some other way. Fix the ownership:

  ```sh
  docker compose run --rm --no-deps --user root --entrypoint chown web -R app:app /data
  ```

- **Login throttling.**
  - 10 failed logins from one IP for one username, or 50 from one IP across all usernames, block logins for 15 minutes.
  - Restarting the container clears this.
  - The first login attempt is logged with its client IP. If every login seems to come from the same Docker gateway address, Docker is hiding real client IPs, and the limits become effectively per-username.

## Development

You need Python 3.14 and [uv](https://docs.astral.sh/uv/) for the tests, and Docker Compose v2.24 or newer for `tests/smoke.sh`.

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python --only-binary :all: -r requirements-dev.txt
.venv/bin/pytest                     # the test suite
./tests/smoke.sh                     # end-to-end Docker check (own volume, port 17777)

# run locally on http://127.0.0.1:7777 (this machine only) with data in ./data
export FLASK_APP=forkful:create_app FORKFUL_DATA_DIR=data
.venv/bin/flask migrate && .venv/bin/flask create-user me
.venv/bin/waitress-serve --host=127.0.0.1 --port=7777 --call forkful:create_app
```

The local server can't share port 7777 with the Docker one; stop one, or give the other a different `--port`.

## License and credits

Forkful is released under the MIT License; see [LICENSE](LICENSE).

The Fredoka font is © The Fredoka Project Authors, licensed under the SIL Open Font License; see [forkful/static/fonts/OFL.txt](forkful/static/fonts/OFL.txt).
