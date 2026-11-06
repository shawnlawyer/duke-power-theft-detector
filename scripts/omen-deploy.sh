#!/bin/sh

set -eu

ROOT_DIR="$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)"
ENV_FILE="${OMEN_ENV_FILE:-$ROOT_DIR/.env.omen}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

command="${1:-all}"

quote() {
  python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$1"
}

die() {
  echo "Error: $*" >&2
  exit 1
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command rsync
require_command ssh
require_command scp
require_command curl
require_command python3

DEFAULT_APP_SECRET="local-power-data-only"
OMEN_HOST="${OMEN_HOST:-192.168.1.120}"
OMEN_SSH_USER="${OMEN_SSH_USER:-shawn}"
OMEN_SSH_KEY="${OMEN_SSH_KEY:-$HOME/.ssh/omen_id_ed25519}"
# Host keys are pinned on first use (accept-new) into a dedicated file so a
# changed Omen host key fails loudly instead of being silently accepted.
OMEN_KNOWN_HOSTS="${OMEN_KNOWN_HOSTS:-$ROOT_DIR/.tmp-deploy/omen_known_hosts}"
OMEN_REMOTE_ROOT="${OMEN_REMOTE_ROOT:-/home/shawn/projects/home-energy-watch}"
OMEN_REMOTE_RUNTIME="${OMEN_REMOTE_RUNTIME:-$OMEN_REMOTE_ROOT/runtime}"
OMEN_BIND_IP="${OMEN_BIND_IP:-$OMEN_HOST}"
OMEN_HOST_PORT="${OMEN_HOST_PORT:-8089}"
OMEN_CONTAINER_NAME="${OMEN_CONTAINER_NAME:-home-energy-watch}"
OMEN_IMAGE_TAG="${OMEN_IMAGE_TAG:-home-energy-watch:local}"
OMEN_HEALTH_RETRIES="${OMEN_HEALTH_RETRIES:-30}"
OMEN_HEALTH_DELAY_SECONDS="${OMEN_HEALTH_DELAY_SECONDS:-2}"
OMEN_HEALTH_MAX_TIME="${OMEN_HEALTH_MAX_TIME:-5}"
POWER_ENV="${POWER_ENV:-staging}"
POWER_APP_SECRET="${POWER_APP_SECRET:-}"
POWER_AUDIT_SIGNING_KEY="${POWER_AUDIT_SIGNING_KEY:-}"
POWER_DATA_ENCRYPTION_KEY="${POWER_DATA_ENCRYPTION_KEY:-}"
POWER_PUBLIC_BASE_URL="${POWER_PUBLIC_BASE_URL:-http://${OMEN_BIND_IP}:${OMEN_HOST_PORT}}"
POWER_MARKETING_BASE_URL="${POWER_MARKETING_BASE_URL:-}"
POWER_TRUST_PROXY="${POWER_TRUST_PROXY:-false}"
POWER_STAFF_MFA_REQUIRED="${POWER_STAFF_MFA_REQUIRED:-false}"
POWER_DATA_DELETION_ENABLED="${POWER_DATA_DELETION_ENABLED:-false}"
POWER_DATA_DELETION_POLICY_VERSION="${POWER_DATA_DELETION_POLICY_VERSION:-}"
POWER_EMAIL_BACKEND="${POWER_EMAIL_BACKEND:-disabled}"
POWER_EMAIL_FROM="${POWER_EMAIL_FROM:-}"
POWER_EMAIL_REPLY_TO="${POWER_EMAIL_REPLY_TO:-}"
POWER_EMAIL_REGION="${POWER_EMAIL_REGION:-us-east-1}"
POWER_BILLING_ENABLED="${POWER_BILLING_ENABLED:-false}"
POWER_WEB_CONCURRENCY="${POWER_WEB_CONCURRENCY:-2}"
POWER_GUNICORN_TIMEOUT="${POWER_GUNICORN_TIMEOUT:-120}"
STRIPE_ACCOUNT_ID="${STRIPE_ACCOUNT_ID:-}"
STRIPE_SECRET_KEY="${STRIPE_SECRET_KEY:-}"
STRIPE_WEBHOOK_SECRET="${STRIPE_WEBHOOK_SECRET:-}"
STRIPE_API_VERSION="${STRIPE_API_VERSION:-2026-02-25.clover}"
STRIPE_PRICE_HOME="${STRIPE_PRICE_HOME:-}"
STRIPE_PRICE_REVIEW="${STRIPE_PRICE_REVIEW:-}"

mkdir -p "$(dirname "$OMEN_KNOWN_HOSTS")"

SSH_TARGET="$OMEN_SSH_USER@$OMEN_HOST"
# Single string for rsync -e; rsync splits it honoring the shell quoting.
RSYNC_SSH="ssh -i $(quote "$OMEN_SSH_KEY") -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$(quote "$OMEN_KNOWN_HOSTS")"
REMOTE_ROOT_Q="$(quote "$OMEN_REMOTE_ROOT")"
REMOTE_RUNTIME_Q="$(quote "$OMEN_REMOTE_RUNTIME")"
REMOTE_ENV_FILE="$OMEN_REMOTE_RUNTIME/.omen-deploy.env"
REMOTE_ENV_FILE_Q="$(quote "$REMOTE_ENV_FILE")"
IMAGE_TAG_Q="$(quote "$OMEN_IMAGE_TAG")"
CONTAINER_Q="$(quote "$OMEN_CONTAINER_NAME")"
PORT_MAP_Q="$(quote "${OMEN_BIND_IP}:${OMEN_HOST_PORT}:8000")"
INPUT_VOLUME_Q="$(quote "${OMEN_REMOTE_RUNTIME}/input:/data/input")"
OUTPUT_VOLUME_Q="$(quote "${OMEN_REMOTE_RUNTIME}/output:/data/output")"
HEALTH_URL="http://${OMEN_BIND_IP}:${OMEN_HOST_PORT}/health"
APP_URL="http://${OMEN_BIND_IP}:${OMEN_HOST_PORT}/"
LOCAL_ENV_FILE=""

cleanup_local_env_file() {
  if [ -n "$LOCAL_ENV_FILE" ]; then
    rm -f "$LOCAL_ENV_FILE"
  fi
}
trap cleanup_local_env_file EXIT
trap 'cleanup_local_env_file; exit 130' INT TERM

remote_run() {
  ssh -i "$OMEN_SSH_KEY" \
    -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=$OMEN_KNOWN_HOSTS" \
    "$SSH_TARGET" "$1"
}

remote_copy() {
  scp -q -i "$OMEN_SSH_KEY" \
    -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=$OMEN_KNOWN_HOSTS" \
    "$1" "$SSH_TARGET:$2"
}

require_app_secret() {
  if [ -z "$POWER_APP_SECRET" ]; then
    die "POWER_APP_SECRET is not set. Put a unique value of at least 32 characters in $ENV_FILE."
  fi
  if [ "$POWER_APP_SECRET" = "$DEFAULT_APP_SECRET" ]; then
    die "POWER_APP_SECRET is the public default '$DEFAULT_APP_SECRET'. Set a unique value of at least 32 characters in $ENV_FILE."
  fi
  if [ "${#POWER_APP_SECRET}" -lt 32 ]; then
    die "POWER_APP_SECRET must be at least 32 characters."
  fi
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
    --exclude '.tmp-*' \
    --exclude 'data/' \
    --exclude 'runtime/' \
    -e "$RSYNC_SSH" \
    "$ROOT_DIR"/ \
    "$SSH_TARGET:$OMEN_REMOTE_ROOT/"
}

seed_runtime_if_empty() {
  ensure_remote_dirs
  if [ -d "$ROOT_DIR/data/input" ]; then
    rsync -az --ignore-existing \
      -e "$RSYNC_SSH" \
      "$ROOT_DIR/data/input"/ \
      "$SSH_TARGET:$OMEN_REMOTE_RUNTIME/input/"
  fi
  if [ -d "$ROOT_DIR/data/output" ]; then
    rsync -az --ignore-existing \
      --include 'power-history.db' \
      --include '*/' \
      --exclude '*' \
      -e "$RSYNC_SSH" \
      "$ROOT_DIR/data/output"/ \
      "$SSH_TARGET:$OMEN_REMOTE_RUNTIME/output/"
  fi
}

env_line() {
  case "$2" in
    *'
'*)
      die "$1 contains a newline; docker --env-file cannot carry it."
      ;;
  esac
  printf '%s=%s\n' "$1" "$2"
}

# Container environment goes into a private file instead of the ssh/docker
# command line, so secrets never appear in process listings or shell history.
write_local_env_file() {
  mkdir -p "$ROOT_DIR/.tmp-deploy"
  LOCAL_ENV_FILE="$(umask 077 && mktemp "$ROOT_DIR/.tmp-deploy/omen-env.XXXXXX")"
  (
    umask 077
    {
      env_line POWER_INPUT_DIR /data/input
      env_line POWER_OUTPUT_DIR /data/output
      env_line POWER_DB_PATH /data/output/power-history.db
      env_line POWER_ENV "$POWER_ENV"
      env_line POWER_APP_SECRET "$POWER_APP_SECRET"
      env_line POWER_AUDIT_SIGNING_KEY "$POWER_AUDIT_SIGNING_KEY"
      env_line POWER_DATA_ENCRYPTION_KEY "$POWER_DATA_ENCRYPTION_KEY"
      env_line POWER_PUBLIC_BASE_URL "$POWER_PUBLIC_BASE_URL"
      env_line POWER_MARKETING_BASE_URL "$POWER_MARKETING_BASE_URL"
      env_line POWER_TRUST_PROXY "$POWER_TRUST_PROXY"
      env_line POWER_STAFF_MFA_REQUIRED "$POWER_STAFF_MFA_REQUIRED"
      env_line POWER_DATA_DELETION_ENABLED "$POWER_DATA_DELETION_ENABLED"
      env_line POWER_DATA_DELETION_POLICY_VERSION "$POWER_DATA_DELETION_POLICY_VERSION"
      env_line POWER_EMAIL_BACKEND "$POWER_EMAIL_BACKEND"
      env_line POWER_EMAIL_FROM "$POWER_EMAIL_FROM"
      env_line POWER_EMAIL_REPLY_TO "$POWER_EMAIL_REPLY_TO"
      env_line POWER_EMAIL_REGION "$POWER_EMAIL_REGION"
      env_line POWER_BILLING_ENABLED "$POWER_BILLING_ENABLED"
      env_line POWER_WEB_CONCURRENCY "$POWER_WEB_CONCURRENCY"
      env_line POWER_GUNICORN_TIMEOUT "$POWER_GUNICORN_TIMEOUT"
      env_line STRIPE_ACCOUNT_ID "$STRIPE_ACCOUNT_ID"
      env_line STRIPE_SECRET_KEY "$STRIPE_SECRET_KEY"
      env_line STRIPE_WEBHOOK_SECRET "$STRIPE_WEBHOOK_SECRET"
      env_line STRIPE_API_VERSION "$STRIPE_API_VERSION"
      env_line STRIPE_PRICE_HOME "$STRIPE_PRICE_HOME"
      env_line STRIPE_PRICE_REVIEW "$STRIPE_PRICE_REVIEW"
    } >"$LOCAL_ENV_FILE"
  )
  chmod 600 "$LOCAL_ENV_FILE"
}

deploy_remote() {
  require_app_secret
  ensure_remote_dirs
  write_local_env_file
  remote_run "umask 077 && : > $REMOTE_ENV_FILE_Q && chmod 600 $REMOTE_ENV_FILE_Q"
  remote_copy "$LOCAL_ENV_FILE" "$REMOTE_ENV_FILE"
  cleanup_local_env_file
  LOCAL_ENV_FILE=""
  # The image runs as an unprivileged user. On Omen the container runs as the
  # SSH user's uid/gid so rsync seeding and the app share ownership of
  # runtime/. A root helper container first takes ownership of anything an
  # earlier root-run container left behind. The env file is removed whether
  # or not the deploy succeeds.
  remote_run "chmod 600 $REMOTE_ENV_FILE_Q; ( set -e; cd $REMOTE_ROOT_Q; docker build -t $IMAGE_TAG_Q .; docker run --rm --user 0:0 -v $INPUT_VOLUME_Q -v $OUTPUT_VOLUME_Q $IMAGE_TAG_Q chown -R \"\$(id -u):\$(id -g)\" /data/input /data/output; docker rm -f $CONTAINER_Q >/dev/null 2>&1 || true; docker run -d --name $CONTAINER_Q --restart unless-stopped --user \"\$(id -u):\$(id -g)\" --env-file $REMOTE_ENV_FILE_Q -p $PORT_MAP_Q -v $INPUT_VOLUME_Q -v $OUTPUT_VOLUME_Q $IMAGE_TAG_Q ); rc=\$?; rm -f $REMOTE_ENV_FILE_Q; exit \$rc"
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
    require_app_secret
    sync_repo
    seed_runtime_if_empty
    deploy_remote
    check_remote
    ;;
  takeover)
    require_app_secret
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
