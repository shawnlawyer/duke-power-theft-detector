#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=/home/ubuntu/home-energy-watch
ENV_FILE="$ROOT_DIR/deploy/ec2/.env.production"
IMAGE_TAG=home-energy-watch:latest

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing production env file: $ENV_FILE" >&2
  exit 1
fi

exec /usr/bin/docker run --rm \
  --name home-energy-watch-utility-sync \
  --init \
  --env-file "$ENV_FILE" \
  -v /opt/home-energy-watch/input:/srv/home-energy-watch/input \
  -v /opt/home-energy-watch/output:/srv/home-energy-watch/output \
  "$IMAGE_TAG" python app.py --sync-utilities
