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

# Read only HEW_IMAGE from the env file without executing it.
configured_image="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?HEW_IMAGE=//p' "$ENV_FILE" | tail -n 1)"
configured_image="${configured_image%\"}"
configured_image="${configured_image#\"}"
configured_image="${configured_image%\'}"
configured_image="${configured_image#\'}"
IMAGE_TAG="${configured_image:-$DEFAULT_IMAGE}"

# Before the first tagged release lands, fall back to the legacy tag.
if ! /usr/bin/docker image inspect "$IMAGE_TAG" >/dev/null 2>&1 \
  && /usr/bin/docker image inspect "$LEGACY_IMAGE" >/dev/null 2>&1; then
  IMAGE_TAG="$LEGACY_IMAGE"
fi

exec /usr/bin/docker run --rm \
  --name home-energy-watch-utility-sync \
  --init \
  --env-file "$ENV_FILE" \
  -v /opt/home-energy-watch/input:/srv/home-energy-watch/input \
  -v /opt/home-energy-watch/output:/srv/home-energy-watch/output \
  -e POWER_INPUT_DIR=/srv/home-energy-watch/input \
  -e POWER_OUTPUT_DIR=/srv/home-energy-watch/output \
  "$IMAGE_TAG" python app.py --sync-utilities
