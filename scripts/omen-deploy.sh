#!/bin/sh

set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
ENV_FILE="${OMEN_ENV_FILE:-$ROOT_DIR/.env.omen}"
EXAMPLE_ENV_FILE="$ROOT_DIR/.env.omen.example"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

command="${1:-all}"

quote() {
  python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command rsync
require_command ssh
require_command curl
require_command python3

OMEN_HOST="${OMEN_HOST:-192.168.1.120}"
OMEN_SSH_USER="${OMEN_SSH_USER:-shawn}"
OMEN_SSH_KEY="${OMEN_SSH_KEY:-$HOME/.ssh/omen_id_ed25519}"
OMEN_REMOTE_ROOT="${OMEN_REMOTE_ROOT:-/home/shawn/projects/home-energy-watch}"
OMEN_REMOTE_RUNTIME="${OMEN_REMOTE_RUNTIME:-$OMEN_REMOTE_ROOT/runtime}"
OMEN_BIND_IP="${OMEN_BIND_IP:-$OMEN_HOST}"
OMEN_HOST_PORT="${OMEN_HOST_PORT:-8089}"
OMEN_CONTAINER_NAME="${OMEN_CONTAINER_NAME:-home-energy-watch}"
OMEN_IMAGE_TAG="${OMEN_IMAGE_TAG:-home-energy-watch:local}"
OMEN_HEALTH_RETRIES="${OMEN_HEALTH_RETRIES:-30}"
OMEN_HEALTH_DELAY_SECONDS="${OMEN_HEALTH_DELAY_SECONDS:-2}"
OMEN_HEALTH_MAX_TIME="${OMEN_HEALTH_MAX_TIME:-5}"
POWER_APP_SECRET="${POWER_APP_SECRET:-local-power-data-only}"
POWER_PUBLIC_BASE_URL="${POWER_PUBLIC_BASE_URL:-http://${OMEN_BIND_IP}:${OMEN_HOST_PORT}}"
POWER_MARKETING_BASE_URL="${POWER_MARKETING_BASE_URL:-}"
POWER_WEB_CONCURRENCY="${POWER_WEB_CONCURRENCY:-2}"
POWER_GUNICORN_TIMEOUT="${POWER_GUNICORN_TIMEOUT:-120}"
STRIPE_SECRET_KEY="${STRIPE_SECRET_KEY:-}"
STRIPE_WEBHOOK_SECRET="${STRIPE_WEBHOOK_SECRET:-}"
STRIPE_PRICE_HOME="${STRIPE_PRICE_HOME:-}"
STRIPE_PRICE_REVIEW="${STRIPE_PRICE_REVIEW:-}"
STRIPE_PRICE_AGENCY="${STRIPE_PRICE_AGENCY:-}"

SSH_TARGET="$OMEN_SSH_USER@$OMEN_HOST"
SSH_BASE="ssh -i $(quote "$OMEN_SSH_KEY") -o StrictHostKeyChecking=no $SSH_TARGET"
REMOTE_ROOT_Q="$(quote "$OMEN_REMOTE_ROOT")"
REMOTE_RUNTIME_Q="$(quote "$OMEN_REMOTE_RUNTIME")"
IMAGE_TAG_Q="$(quote "$OMEN_IMAGE_TAG")"
CONTAINER_Q="$(quote "$OMEN_CONTAINER_NAME")"
PORT_MAP_Q="$(quote "${OMEN_BIND_IP}:${OMEN_HOST_PORT}:8000")"
INPUT_VOLUME_Q="$(quote "${OMEN_REMOTE_RUNTIME}/input:/data/input")"
OUTPUT_VOLUME_Q="$(quote "${OMEN_REMOTE_RUNTIME}/output:/data/output")"
POWER_APP_SECRET_Q="$(quote "$POWER_APP_SECRET")"
POWER_PUBLIC_BASE_URL_Q="$(quote "$POWER_PUBLIC_BASE_URL")"
POWER_MARKETING_BASE_URL_Q="$(quote "$POWER_MARKETING_BASE_URL")"
POWER_WEB_CONCURRENCY_Q="$(quote "$POWER_WEB_CONCURRENCY")"
POWER_GUNICORN_TIMEOUT_Q="$(quote "$POWER_GUNICORN_TIMEOUT")"
STRIPE_SECRET_KEY_Q="$(quote "$STRIPE_SECRET_KEY")"
STRIPE_WEBHOOK_SECRET_Q="$(quote "$STRIPE_WEBHOOK_SECRET")"
STRIPE_PRICE_HOME_Q="$(quote "$STRIPE_PRICE_HOME")"
STRIPE_PRICE_REVIEW_Q="$(quote "$STRIPE_PRICE_REVIEW")"
STRIPE_PRICE_AGENCY_Q="$(quote "$STRIPE_PRICE_AGENCY")"
HEALTH_URL="http://${OMEN_BIND_IP}:${OMEN_HOST_PORT}/health"
APP_URL="http://${OMEN_BIND_IP}:${OMEN_HOST_PORT}/"

remote_run() {
  ssh -i "$OMEN_SSH_KEY" -o StrictHostKeyChecking=no "$SSH_TARGET" "$1"
}

ensure_remote_dirs() {
  remote_run "mkdir -p $REMOTE_ROOT_Q $REMOTE_RUNTIME_Q/input $REMOTE_RUNTIME_Q/output"
}

sync_repo() {
  ensure_remote_dirs
  rsync -az --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.env.omen' \
    --exclude 'data/' \
    --exclude 'runtime/' \
    -e "ssh -i $OMEN_SSH_KEY -o StrictHostKeyChecking=no" \
    "$ROOT_DIR"/ \
    "$SSH_TARGET:$OMEN_REMOTE_ROOT/"
}

seed_runtime_if_empty() {
  ensure_remote_dirs
  if [ -d "$ROOT_DIR/data/input" ]; then
    rsync -az --ignore-existing \
      -e "ssh -i $OMEN_SSH_KEY -o StrictHostKeyChecking=no" \
      "$ROOT_DIR/data/input"/ \
      "$SSH_TARGET:$OMEN_REMOTE_RUNTIME/input/"
  fi
  if [ -d "$ROOT_DIR/data/output" ]; then
    rsync -az --ignore-existing \
      --include 'power-history.db' \
      --include '*/' \
      --exclude '*' \
      -e "ssh -i $OMEN_SSH_KEY -o StrictHostKeyChecking=no" \
      "$ROOT_DIR/data/output"/ \
      "$SSH_TARGET:$OMEN_REMOTE_RUNTIME/output/"
  fi
}

deploy_remote() {
  ensure_remote_dirs
  remote_run "cd $REMOTE_ROOT_Q && docker build -t $IMAGE_TAG_Q . && (docker rm -f $CONTAINER_Q || true) && docker run -d --name $CONTAINER_Q --restart unless-stopped -e POWER_INPUT_DIR=/data/input -e POWER_OUTPUT_DIR=/data/output -e POWER_DB_PATH=/data/output/power-history.db -e POWER_APP_SECRET=$POWER_APP_SECRET_Q -e POWER_PUBLIC_BASE_URL=$POWER_PUBLIC_BASE_URL_Q -e POWER_MARKETING_BASE_URL=$POWER_MARKETING_BASE_URL_Q -e POWER_WEB_CONCURRENCY=$POWER_WEB_CONCURRENCY_Q -e POWER_GUNICORN_TIMEOUT=$POWER_GUNICORN_TIMEOUT_Q -e STRIPE_SECRET_KEY=$STRIPE_SECRET_KEY_Q -e STRIPE_WEBHOOK_SECRET=$STRIPE_WEBHOOK_SECRET_Q -e STRIPE_PRICE_HOME=$STRIPE_PRICE_HOME_Q -e STRIPE_PRICE_REVIEW=$STRIPE_PRICE_REVIEW_Q -e STRIPE_PRICE_AGENCY=$STRIPE_PRICE_AGENCY_Q -p $PORT_MAP_Q -v $INPUT_VOLUME_Q -v $OUTPUT_VOLUME_Q $IMAGE_TAG_Q"
}

wait_for_health() {
  attempt=1
  while [ "$attempt" -le "$OMEN_HEALTH_RETRIES" ]; do
    if curl --fail --silent --show-error --max-time "$OMEN_HEALTH_MAX_TIME" "$HEALTH_URL"; then
      printf '\n%s\n' "$APP_URL"
      return 0
    fi

    if [ "$attempt" -lt "$OMEN_HEALTH_RETRIES" ]; then
      echo "Waiting for Home Energy Watch health on Omen ($attempt/$OMEN_HEALTH_RETRIES)..."
      sleep "$OMEN_HEALTH_DELAY_SECONDS"
    fi

    attempt=$((attempt + 1))
  done

  echo "Home Energy Watch did not become healthy on Omen after $OMEN_HEALTH_RETRIES attempts." >&2
  remote_run "docker ps --filter name=$CONTAINER_Q --format '{{.ID}} {{.Status}} {{.Ports}}' && docker logs --tail 80 $CONTAINER_Q" || true
  return 1
}

check_remote() {
  remote_run "docker ps --filter name=$CONTAINER_Q --format '{{.ID}} {{.Status}} {{.Ports}}'"
  wait_for_health
}

show_logs() {
  remote_run "docker logs --tail 100 -f $CONTAINER_Q"
}

show_ps() {
  remote_run "docker ps --filter name=$CONTAINER_Q --format '{{.ID}} {{.Image}} {{.Status}} {{.Ports}}'"
}

show_port_owner() {
  remote_run "docker ps --format '{{.Names}} {{.Image}} {{.Status}} {{.Ports}}' | grep ':$OMEN_HOST_PORT->' || true"
}

free_staging_port() {
  remote_run "owners=\$(docker ps --format '{{.Names}} {{.Ports}}' | awk '/:$OMEN_HOST_PORT->/ {print \$1}'); for owner in \$owners; do if [ \"\$owner\" != $CONTAINER_Q ]; then docker stop \"\$owner\"; fi; done"
}

case "$command" in
  sync)
    sync_repo
    ;;
  seed)
    seed_runtime_if_empty
    ;;
  deploy)
    deploy_remote
    ;;
  check)
    check_remote
    ;;
  logs)
    show_logs
    ;;
  ps)
    show_ps
    ;;
  port-owner)
    show_port_owner
    ;;
  take-port)
    free_staging_port
    ;;
  url)
    printf '%s\n' "$APP_URL"
    ;;
  all)
    sync_repo
    seed_runtime_if_empty
    deploy_remote
    check_remote
    ;;
  takeover)
    sync_repo
    seed_runtime_if_empty
    free_staging_port
    deploy_remote
    check_remote
    ;;
  *)
    echo "Usage: $0 {all|takeover|sync|seed|deploy|check|logs|ps|port-owner|take-port|url}" >&2
    exit 1
    ;;
esac
