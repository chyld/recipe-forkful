#!/usr/bin/env bash
# End-to-end check of the real container: build, start, create a user, log in, create a
# recipe with a photo, fork it, download it, survive restart and down/up, check headers and the 413 page.
# Uses a separate project, volume (forkful-smoke-data) and port (17777); cleans up after itself.
set -euo pipefail
cd "$(dirname "$0")/.."

DC=(docker compose -p forkful-smoke -f compose.yaml -f tests/compose.smoke.yaml)
BASE=http://127.0.0.1:17777
WORK=$(mktemp -d)
JAR="$WORK/jar"
PASS="smoke-test-password-1"

SMOKE_VOLUME=forkful-smoke-data
# Never `down -v`: if the override failed to load, that would delete the real forkful-data volume.
cleanup() {
  "${DC[@]}" down >/dev/null 2>&1 || true
  docker volume rm -f "$SMOKE_VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
csrf() { grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//; s/"$//'; }

wait_healthy() {
  for _ in $(seq 1 60); do
    status=$(docker inspect --format '{{.State.Health.Status}}' "$("${DC[@]}" ps -q web)" 2>/dev/null || true)
    [ "$status" = "healthy" ] && return 0
    sleep 1
  done
  "${DC[@]}" logs web | tail -30
  fail "container never became healthy"
}

step "build and start"
"${DC[@]}" config --volumes >/dev/null
[ "$("${DC[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes"]["forkful-data"]["name"])')" = "$SMOKE_VOLUME" ] \
  || fail "the smoke override isn't active; refusing to touch the real volume"
cleanup; WORK=$(mktemp -d); JAR="$WORK/jar"
"${DC[@]}" up -d --build
wait_healthy

step "create a user (password from stdin, no TTY)"
printf '%s\n' "$PASS" | "${DC[@]}" exec -T web flask create-user chef --password-stdin
"${DC[@]}" exec -T web sh -c 'id -u; stat -c "%u %a %n" /data /data/forkful.db /data/secret_key'

step "headers"
headers=$(curl -fsS -D - -o /dev/null "$BASE/login")
grep -qi "content-security-policy: default-src 'self'" <<<"$headers" || fail "no CSP"
grep -qi "x-frame-options: DENY" <<<"$headers" || fail "no X-Frame-Options"
grep -qi "set-cookie:.*HttpOnly" <<<"$headers" || fail "session cookie not HttpOnly"

step "log in"
token=$(curl -fsS -c "$JAR" -b "$JAR" "$BASE/login" | csrf)
code=$(curl -sS -o /dev/null -w '%{http_code}' -c "$JAR" -b "$JAR" \
  --data-urlencode "csrf_token=$token" --data-urlencode "username=chef" --data-urlencode "password=$PASS" "$BASE/login")
[ "$code" = 302 ] || fail "login returned $code"

step "create a recipe with a photo"
"${DC[@]}" exec -T web python -c \
  "import sys; from PIL import Image; Image.new('RGB', (640, 480), 'tomato').save(sys.stdout.buffer, 'JPEG')" > "$WORK/photo.jpg"
token=$(curl -fsS -c "$JAR" -b "$JAR" "$BASE/recipes/new" | csrf)
location=$(curl -sS -o /dev/null -w '%{redirect_url}' -c "$JAR" -b "$JAR" \
  -F "csrf_token=$token" -F "name=Smoke-test shakshuka" -F "instructions=Simmer. Crack eggs. Cover." \
  -F "ing-0-name=Eggs" -F "ing-0-amount=4" -F "ing-0-calories=280" \
  -F "ing-1-name=Tomatoes" -F "ing-1-amount=1 can" -F "ing-1-calories=90" \
  -F "photos=@$WORK/photo.jpg;type=image/jpeg" "$BASE/recipes/new")
[[ "$location" =~ /recipes/([0-9a-z]{12})$ ]] || fail "create did not redirect to a recipe: $location"
recipe="${BASH_REMATCH[1]}"
page=$(curl -fsS -b "$JAR" "$BASE/recipes/$recipe")
grep -q "Smoke-test shakshuka" <<<"$page" || fail "recipe page missing name"
grep -q ">370<" <<<"$page" || fail "calorie total wrong"
photo=$(grep -o '/photos/[0-9a-f]\{32\}' <<<"$page" | head -1)
[ -n "$photo" ] || fail "no photo on the page"
curl -fsS -b "$JAR" -D "$WORK/photo-headers" -o "$WORK/served.jpg" "$BASE$photo"
grep -qi "cache-control: private, no-cache" "$WORK/photo-headers" || fail "photo cache headers wrong"
[ "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE$photo")" = 302 ] || fail "photo served without login"

step "fork it"
token=$(curl -fsS -b "$JAR" "$BASE/recipes/$recipe" | csrf)
location=$(curl -sS -o /dev/null -w '%{redirect_url}' -c "$JAR" -b "$JAR" \
  --data-urlencode "csrf_token=$token" --data-urlencode "name=Smoke-test shakshuka remix" "$BASE/recipes/$recipe/fork")
[[ "$location" =~ /recipes/([0-9a-z]{12})$ ]] || fail "fork did not redirect: $location"
fork="${BASH_REMATCH[1]}"
curl -fsS -b "$JAR" "$BASE/recipes/$fork" | grep -q "Remix of Smoke-test shakshuka" || fail "fork lost its parent"

step "download it as one HTML file"
curl -fsS -b "$JAR" -D "$WORK/download-headers" -o "$WORK/recipe.html" "$BASE/recipes/$recipe/download"
grep -qi "content-disposition: attachment; filename=smoke-test-shakshuka.html" "$WORK/download-headers" || fail "download is not an attachment"
grep -q "data:image/jpeg;base64," "$WORK/recipe.html" || fail "the photo isn't inside the downloaded file"
grep -q "data:font/woff2;base64," "$WORK/recipe.html" || fail "the font isn't inside the downloaded file"
! grep -q "/photos/\|/static/" "$WORK/recipe.html" || fail "the downloaded file links back to the server"

step "oversized upload gets the friendly 413 (between Flask's and Waitress's limits)"
head -c 210000000 /dev/zero > "$WORK/big.bin"
token=$(curl -fsS -b "$JAR" "$BASE/recipes/new" | csrf)
code=$(curl -sS -o "$WORK/413.html" -w '%{http_code}' -b "$JAR" \
  -F "csrf_token=$token" -F "name=Too big" -F "photos=@$WORK/big.bin;type=image/jpeg" "$BASE/recipes/new")
[ "$code" = 413 ] || fail "oversized upload returned $code"
grep -q "nothing was saved" "$WORK/413.html" || fail "413 page is not the friendly one"

step "restart keeps everything"
"${DC[@]}" restart web
wait_healthy
curl -fsS -b "$JAR" "$BASE/" | grep -q "Smoke-test shakshuka remix" || fail "data lost after restart"

step "down + up (volume kept) keeps everything"
"${DC[@]}" down
"${DC[@]}" up -d
wait_healthy
curl -fsS -b "$JAR" "$BASE/recipes/$recipe" | grep -q "$photo" || fail "photo lost after down/up"

step "set-password logs the user out"
printf '%s\n' "another-password-2" | "${DC[@]}" exec -T web flask set-password chef --password-stdin
[ "$(curl -sS -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/")" = 302 ] || fail "old session survived set-password"

printf '\nSMOKE TEST PASSED\n'
