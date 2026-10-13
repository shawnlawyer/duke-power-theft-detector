#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=/home/ubuntu/home-energy-watch
ENV_FILE="$ROOT_DIR/deploy/ec2/.env.production"
DEFAULT_IMAGE=home-energy-watch:current
LEGACY_IMAGE=home-energy-watch:latest

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing production env file: $ENV_FILE" >&2
  exit 1
fi

RUNTIME_ENV_DIR=/run/home-energy-watch
RUNTIME_ENV_FILE="$RUNTIME_ENV_DIR/sync.env"

# Read the env file the same way home-energy-watch-direct.sh does (bash
# semantics: quotes, inline comments), then hand docker an env file built from
# the sourced values. Passing .env.production straight to docker --env-file
# would keep quote characters in values and give this job different secrets
# than the web container.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
IMAGE_TAG="${HEW_IMAGE:-$DEFAULT_IMAGE}"

umask 077
mkdir -p "$RUNTIME_ENV_DIR"
chmod 700 "$RUNTIME_ENV_DIR"
tmp_env_file="$(mktemp "$RUNTIME_ENV_DIR/sync.env.XXXXXX")"
env_names="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/p' "$ENV_FILE" | sort -u)"
while IFS= read -r name; do
  [[ -n "$name" ]] || continue
  value="${!name-}"
  if [[ "$value" == *$'\n'* ]]; then
    rm -f "$tmp_env_file"
    echo "Env variable $name contains a newline; docker --env-file cannot carry it." >&2
    exit 1
  fi
  printf '%s=%s\n' "$name" "$value" >>"$tmp_env_file"
done <<<"$env_names"
mv -f "$tmp_env_file" "$RUNTIME_ENV_FILE"

# Before the first tagged release lands, fall back to the legacy tag.
if ! /usr/bin/docker image inspect "$IMAGE_TAG" >/dev/null 2>&1 \
  && /usr/bin/docker image inspect "$LEGACY_IMAGE" >/dev/null 2>&1; then
  IMAGE_TAG="$LEGACY_IMAGE"
fi

exec /usr/bin/docker run --rm \
  --name home-energy-watch-utility-sync \
  --init \
  --env-file "$RUNTIME_ENV_FILE" \
  -v /opt/home-energy-watch/input:/srv/home-energy-watch/input \
  -v /opt/home-energy-watch/output:/srv/home-energy-watch/output \
  -e POWER_INPUT_DIR=/srv/home-energy-watch/input \
  -e POWER_OUTPUT_DIR=/srv/home-energy-watch/output \
  "$IMAGE_TAG" python app.py --sync-utilities
