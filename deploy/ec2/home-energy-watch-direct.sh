#!/usr/bin/env bash
# Started by home-energy-watch.service (systemd, as root).
#
# Runs the already-built release image. Images are built and tagged by
# scripts/deploy-production.sh (home-energy-watch:<git-sha>, :current,
# :previous). This script only builds when the selected image is missing
# locally, as a first-boot fallback, so a systemd restart never rebuilds.
set -euo pipefail

ROOT_DIR=/home/ubuntu/home-energy-watch
ENV_FILE="$ROOT_DIR/deploy/ec2/.env.production"
DEFAULT_IMAGE=home-energy-watch:current
CONTAINER_NAME=home-energy-watch
INPUT_DIR=/opt/home-energy-watch/input
OUTPUT_DIR=/opt/home-energy-watch/output
# Root-only tmpfs directory for the container env file generated below.
RUNTIME_ENV_DIR=/run/home-energy-watch
RUNTIME_ENV_FILE="$RUNTIME_ENV_DIR/web.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing production env file: $ENV_FILE" >&2
  exit 1
fi

mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"

# Read the variables this script itself needs (image tag, port, bind address)
# plus every container variable by sourcing the env file in bash, exactly as
# earlier releases did. Then hand the container a docker --env-file built from
# the sourced values. Building the env file from sourced values (rather than
# passing .env.production to docker directly) keeps the existing file's bash
# semantics (quoting, inline comments) working unchanged, keeps secrets off
# the docker command line, and passes every variable defined in the file, the
# same set the utility-sync job receives.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

HEW_IMAGE="${HEW_IMAGE:-$DEFAULT_IMAGE}"
POWER_WEB_PORT="${POWER_WEB_PORT:-80}"
POWER_BIND_ADDRESS="${POWER_BIND_ADDRESS:-}"

if [[ -n "$POWER_BIND_ADDRESS" ]]; then
  PUBLISH="${POWER_BIND_ADDRESS}:${POWER_WEB_PORT}:8000"
else
  # Unset keeps the historical behavior: publish on every host interface.
  PUBLISH="${POWER_WEB_PORT}:8000"
fi

umask 077
mkdir -p "$RUNTIME_ENV_DIR"
chmod 700 "$RUNTIME_ENV_DIR"
tmp_env_file="$(mktemp "$RUNTIME_ENV_DIR/web.env.XXXXXX")"
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

if ! docker image inspect "$HEW_IMAGE" >/dev/null 2>&1; then
  echo "Image $HEW_IMAGE not found locally; building it once from $ROOT_DIR." >&2
  docker build -t "$HEW_IMAGE" "$ROOT_DIR"
fi

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

# Health: the image defines a Docker HEALTHCHECK against /health.
# Later -e values override the same names from --env-file.
exec docker run \
  --name "$CONTAINER_NAME" \
  --init \
  -p "$PUBLISH" \
  -v "$INPUT_DIR:/srv/home-energy-watch/input" \
  -v "$OUTPUT_DIR:/srv/home-energy-watch/output" \
  --env-file "$RUNTIME_ENV_FILE" \
  -e POWER_INPUT_DIR=/srv/home-energy-watch/input \
  -e POWER_OUTPUT_DIR=/srv/home-energy-watch/output \
  "$HEW_IMAGE"
