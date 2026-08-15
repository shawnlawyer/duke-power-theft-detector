#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=/home/ubuntu/home-energy-watch
ENV_FILE="$ROOT_DIR/deploy/ec2/.env.production"
IMAGE_TAG=home-energy-watch:latest
CONTAINER_NAME=home-energy-watch
INPUT_DIR=/opt/home-energy-watch/input
OUTPUT_DIR=/opt/home-energy-watch/output

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing production env file: $ENV_FILE" >&2
  exit 1
fi

mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$ROOT_DIR"
docker build -t "$IMAGE_TAG" .
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

exec docker run \
  --name "$CONTAINER_NAME" \
  --init \
  -p "${POWER_WEB_PORT:-80}:8000" \
  -v "$INPUT_DIR:/srv/home-energy-watch/input" \
  -v "$OUTPUT_DIR:/srv/home-energy-watch/output" \
  -e POWER_DATABASE_URL \
  -e POWER_ENV \
  -e POWER_APP_SECRET \
  -e POWER_AUDIT_SIGNING_KEY \
  -e POWER_DATA_ENCRYPTION_KEY \
  -e POWER_PUBLIC_BASE_URL \
  -e POWER_MARKETING_BASE_URL \
  -e POWER_TRUST_PROXY \
  -e POWER_LOG_LEVEL \
  -e POWER_LOG_FORMAT \
  -e POWER_STAFF_MFA_REQUIRED \
  -e POWER_DATA_DELETION_ENABLED \
  -e POWER_DATA_DELETION_POLICY_VERSION \
  -e POWER_EMAIL_BACKEND \
  -e POWER_EMAIL_FROM \
  -e POWER_EMAIL_REPLY_TO \
  -e POWER_EMAIL_REGION \
  -e POWER_BILLING_ENABLED \
  -e STRIPE_ACCOUNT_ID \
  -e STRIPE_SECRET_KEY \
  -e STRIPE_WEBHOOK_SECRET \
  -e STRIPE_API_VERSION \
  -e STRIPE_PRICE_HOME \
  -e STRIPE_PRICE_REVIEW \
  -e POWER_INPUT_DIR=/srv/home-energy-watch/input \
  -e POWER_OUTPUT_DIR=/srv/home-energy-watch/output \
  -e POWER_TIMEZONE \
  -e POWER_NIGHT_START \
  -e POWER_NIGHT_END \
  -e POWER_MIN_NIGHT_KW \
  -e POWER_NIGHT_MULTIPLIER \
  -e POWER_ALERT_WINDOW_START \
  -e POWER_ALERT_WINDOW_END \
  -e POWER_ALERT_MIN_KW \
  -e POWER_ALERT_MULTIPLIER \
  -e POWER_ALERT_JUMP_KW \
  -e POWER_WEB_CONCURRENCY \
  -e POWER_GUNICORN_TIMEOUT \
  "$IMAGE_TAG"
