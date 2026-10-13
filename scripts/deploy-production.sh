#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${PRODUCTION_ARTIFACT_DIR:-$ROOT_DIR/.tmp-deploy}"
LOCAL_IMAGE_TAG="${PRODUCTION_LOCAL_IMAGE_TAG:-home-energy-watch:production-package}"
DEFAULT_PYTHON_BIN="python3"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  DEFAULT_PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON_BIN}"
REMOTE_ROOT="${PRODUCTION_REMOTE_ROOT:-/home/ubuntu/home-energy-watch}"
REMOTE_BACKUP_DIR="${PRODUCTION_REMOTE_BACKUP_DIR:-/home/ubuntu/home-energy-watch-backups}"
REMOTE_RELEASE_DIR="${PRODUCTION_REMOTE_RELEASE_DIR:-/home/ubuntu/home-energy-watch-releases}"
REMOTE_SERVICE="${PRODUCTION_SERVICE:-home-energy-watch}"
RELEASE_BRANCH="${PRODUCTION_RELEASE_BRANCH:-main}"
RELEASE_REMOTE_REF="${PRODUCTION_RELEASE_REMOTE_REF:-origin/main}"
REMOTE_ENV_FILE_RELATIVE="deploy/ec2/.env.production"
HEALTH_URL="${PRODUCTION_HEALTH_URL:-https://app.homeenergywatch.com/health}"
HEALTH_RETRIES="${PRODUCTION_HEALTH_RETRIES:-30}"
HEALTH_DELAY_SECONDS="${PRODUCTION_HEALTH_DELAY_SECONDS:-2}"
HEALTH_MAX_TIME="${PRODUCTION_HEALTH_MAX_TIME:-5}"
REMOTE_IMAGE_REPO="${PRODUCTION_IMAGE_REPO:-home-energy-watch}"
REMOTE_DATA_DIRS="${PRODUCTION_REMOTE_DATA_DIRS:-/opt/home-energy-watch/input /opt/home-energy-watch/output}"
# UID/GID of the unprivileged "app" user baked into the Dockerfile.
CONTAINER_UID="10001"
RDS_INSTANCE_ID="${HEW_RDS_INSTANCE_ID:-}"
PACKAGE_ARCHIVE=""
RELEASE_SHA=""

cd "$ROOT_DIR"

command="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

confirm_production=false
skip_tests=false
requested_backup=""

log() {
  printf '%s\n' "==> $*" >&2
}

die() {
  printf '%s\n' "Error: $*" >&2
  exit 1
}

remote_quote() {
  local value="$1"
  printf "'%s'" "${value//\'/\'\"\'\"\'}"
}

show_help() {
  cat <<'HELP'
Home Energy Watch production deployment helper

Usage:
  scripts/deploy-production.sh check
  scripts/deploy-production.sh package [--skip-tests]
  scripts/deploy-production.sh deploy --confirm-production [--skip-tests]
  scripts/deploy-production.sh rollback --confirm-production [--backup BACKUP_FILE]
  scripts/deploy-production.sh help

Commands:
  check       Run local syntax, test, diff, and Docker build checks. If
              PRODUCTION_SSH_TARGET is set, also checks remote prerequisites.
  package     Run tests by default, build the Docker image, and create a source
              archive under .tmp-deploy without changing production.
  deploy      Package, optionally snapshot RDS (HEW_RDS_INSTANCE_ID), copy the
              archive to the production EC2 host, back up the current release,
              replace only application source, build the image once tagged
              home-energy-watch:<git-sha>, retag the running image as
              :previous and the new one as :current, restart the
              home-energy-watch systemd service, and verify /health. If the
              restart or health verification fails, :previous is retagged as
              :current, the service is restarted and re-verified, and the
              command exits non-zero.
  rollback    Restore the latest source backup, or the backup named with
              --backup, rebuild and tag it as :current, restart the
              home-energy-watch service, and verify /health.
  help        Show this message.

Production-changing commands require:
  --confirm-production

Environment:
  PRODUCTION_SSH_TARGET       Required for deploy/rollback, for example ubuntu@203.0.113.10
  PRODUCTION_SSH_KEY          Optional SSH private key path
  PRODUCTION_SSH_PORT         Optional SSH port
  PRODUCTION_REMOTE_ROOT      Defaults to /home/ubuntu/home-energy-watch
  PRODUCTION_REMOTE_BACKUP_DIR Defaults to /home/ubuntu/home-energy-watch-backups
  PRODUCTION_REMOTE_RELEASE_DIR Defaults to /home/ubuntu/home-energy-watch-releases
  PRODUCTION_SERVICE          Defaults to home-energy-watch
  PRODUCTION_RELEASE_BRANCH   Defaults to main
  PRODUCTION_RELEASE_REMOTE_REF Defaults to origin/main
  PRODUCTION_HEALTH_URL       Defaults to https://app.homeenergywatch.com/health
  PRODUCTION_IMAGE_REPO       Defaults to home-energy-watch (tags :<sha>, :current, :previous)
  PRODUCTION_REMOTE_DATA_DIRS Host bind-mount dirs chowned to the container
                              user (uid 10001) on every deploy. Defaults to
                              "/opt/home-energy-watch/input /opt/home-energy-watch/output"
  HEW_RDS_INSTANCE_ID         Optional. When set, deploy first runs
                              aws rds create-db-snapshot for this instance and
                              waits until the snapshot is available. Uses the
                              caller's AWS CLI profile/region.
  PYTHON_BIN                  Defaults to .venv/bin/python when present, otherwise python3

The deployment archive contains only files tracked by the clean, merged Git
commit. Keep deploy/ec2/.env.production on the production host or in an
approved secret store; it must never be tracked by Git.
HELP
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm-production)
      confirm_production=true
      ;;
    --skip-tests)
      skip_tests=true
      ;;
    --backup)
      [[ $# -ge 2 ]] || die "--backup requires a backup file name"
      requested_backup="$2"
      shift
      ;;
    -h|--help)
      command="help"
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done

case "$command" in
  -h|--help)
    command="help"
    ;;
esac

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "Missing required command: $1"
  fi
}

require_python() {
  if [[ -x "$PYTHON_BIN" ]]; then
    return
  fi
  if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    return
  fi
  die "Missing Python interpreter: $PYTHON_BIN"
}

run_syntax_checks() {
  log "Running Python syntax checks"
  PYTHONPATH=. "$PYTHON_BIN" -m py_compile app.py tests/test_app.py tests/test_omen_deploy_contract.py tests/test_release_security_contract.py
  if [[ -f tests/test_production_deploy_contract.py ]]; then
    PYTHONPATH=. "$PYTHON_BIN" -m py_compile tests/test_production_deploy_contract.py
  fi

  log "Checking shell script syntax"
  bash -n "$ROOT_DIR/scripts/deploy-production.sh"
  bash -n "$ROOT_DIR/deploy/ec2/home-energy-watch-direct.sh"
  bash -n "$ROOT_DIR/deploy/ec2/home-energy-watch-utility-sync.sh"
}

run_tests() {
  if [[ "$skip_tests" == "true" ]]; then
    log "Skipping tests because --skip-tests was supplied"
    return
  fi
  log "Running pytest"
  PYTHONPATH=. "$PYTHON_BIN" -m pytest
}

run_diff_check() {
  if command -v git >/dev/null 2>&1 && git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "Running git diff whitespace check"
    git -C "$ROOT_DIR" diff --check
  fi
}

require_release_commit() {
  require_command git
  git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "Production releases must come from a Git checkout."

  local current_branch
  current_branch="$(git -C "$ROOT_DIR" branch --show-current)"
  [[ "$current_branch" == "$RELEASE_BRANCH" ]] \
    || die "Production releases must run from $RELEASE_BRANCH, not ${current_branch:-detached HEAD}."

  [[ -z "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=all)" ]] \
    || die "Commit or remove every local change before packaging production."

  git -C "$ROOT_DIR" rev-parse --verify "$RELEASE_REMOTE_REF^{commit}" >/dev/null 2>&1 \
    || die "Fetch $RELEASE_REMOTE_REF before packaging production."

  local local_revision
  local remote_revision
  local_revision="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  remote_revision="$(git -C "$ROOT_DIR" rev-parse "$RELEASE_REMOTE_REF^{commit}")"
  [[ "$local_revision" == "$remote_revision" ]] \
    || die "Local $RELEASE_BRANCH must exactly match $RELEASE_REMOTE_REF before packaging production."
}

build_project() {
  require_command docker
  log "Building Docker image with the repository Dockerfile"
  docker build -t "$LOCAL_IMAGE_TAG" "$ROOT_DIR"
}

build_id() {
  local timestamp
  local revision
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  revision="nogit"
  if command -v git >/dev/null 2>&1 && git -C "$ROOT_DIR" rev-parse --short HEAD >/dev/null 2>&1; then
    revision="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
  fi
  printf '%s-%s' "$timestamp" "$revision"
}

release_sha() {
  git -C "$ROOT_DIR" rev-parse --short=12 HEAD
}

create_archive() {
  mkdir -p "$ARTIFACT_DIR"
  local id
  local archive
  id="$(build_id)"
  archive="$ARTIFACT_DIR/home-energy-watch-$id.tgz"

  log "Creating deployable archive from the merged commit"
  git -C "$ROOT_DIR" archive --format=tar.gz --output="$archive" HEAD

  printf '%s\n' "$archive"
}

package_release() {
  run_syntax_checks
  run_tests
  run_diff_check
  build_project
  PACKAGE_ARCHIVE="$(create_archive)"
  printf '%s\n' "$PACKAGE_ARCHIVE"
}

build_ssh_args() {
  SSH_ARGS=(-o BatchMode=yes)
  SCP_ARGS=()
  if [[ -n "${PRODUCTION_SSH_KEY:-}" ]]; then
    SSH_ARGS+=(-i "$PRODUCTION_SSH_KEY")
    SCP_ARGS+=(-i "$PRODUCTION_SSH_KEY")
  fi
  if [[ -n "${PRODUCTION_SSH_PORT:-}" ]]; then
    SSH_ARGS+=(-p "$PRODUCTION_SSH_PORT")
    SCP_ARGS+=(-P "$PRODUCTION_SSH_PORT")
  fi
}

require_production_confirmation() {
  if [[ "$confirm_production" != "true" ]]; then
    die "$command changes production. Re-run with --confirm-production after verifying the target."
  fi
}

require_remote_target() {
  [[ -n "${PRODUCTION_SSH_TARGET:-}" ]] || die "Set PRODUCTION_SSH_TARGET, for example ubuntu@ec2-host"
  build_ssh_args
}

remote_run() {
  ssh "${SSH_ARGS[@]}" "$PRODUCTION_SSH_TARGET" "$@"
}

remote_copy() {
  local source="$1"
  local destination="$2"
  scp "${SCP_ARGS[@]}" "$source" "$PRODUCTION_SSH_TARGET:$destination"
}

check_remote_prerequisites() {
  require_remote_target
  log "Checking remote production prerequisites"
  remote_run "command -v docker >/dev/null && command -v rsync >/dev/null && command -v tar >/dev/null && command -v curl >/dev/null && command -v sudo >/dev/null && test -f $(remote_quote "$REMOTE_ROOT/$REMOTE_ENV_FILE_RELATIVE")"
}

check_health() {
  log "Verifying runtime health"
  local attempt
  attempt=1
  while [[ "$attempt" -le "$HEALTH_RETRIES" ]]; do
    if curl --fail --silent --show-error --max-time "$HEALTH_MAX_TIME" "$HEALTH_URL" >/dev/null; then
      printf '%s\n' "Health check passed: $HEALTH_URL"
      return 0
    fi
    if [[ "$attempt" -lt "$HEALTH_RETRIES" ]]; then
      sleep "$HEALTH_DELAY_SECONDS"
    fi
    attempt=$((attempt + 1))
  done
  printf '%s\n' "Health check failed after $HEALTH_RETRIES attempts: $HEALTH_URL" >&2
  return 1
}

verify_health() {
  check_health || die "Health check failed after $HEALTH_RETRIES attempts: $HEALTH_URL"
}

snapshot_rds_before_deploy() {
  if [[ -z "$RDS_INSTANCE_ID" ]]; then
    log "Skipping pre-deploy RDS snapshot because HEW_RDS_INSTANCE_ID is not set"
    return 0
  fi
  require_command aws
  local snapshot_id
  snapshot_id="home-energy-watch-predeploy-$(date -u +%Y%m%d-%H%M%S)-${RELEASE_SHA:-nogit}"
  log "Creating pre-deploy RDS snapshot $snapshot_id of $RDS_INSTANCE_ID"
  aws rds create-db-snapshot \
    --db-instance-identifier "$RDS_INSTANCE_ID" \
    --db-snapshot-identifier "$snapshot_id" >/dev/null \
    || die "RDS snapshot request failed for $RDS_INSTANCE_ID; production was not changed."
  log "Waiting for RDS snapshot $snapshot_id to become available"
  aws rds wait db-snapshot-available --db-snapshot-identifier "$snapshot_id" \
    || die "RDS snapshot $snapshot_id did not become available; production was not changed."
  printf '%s\n' "RDS snapshot available: $snapshot_id"
}

deploy_archive() {
  local archive="$1"
  local remote_archive
  remote_archive="/tmp/$(basename "$archive")"
  require_remote_target
  log "Copying release archive to production host"
  remote_copy "$archive" "$remote_archive"

  log "Installing release source and building image $REMOTE_IMAGE_REPO:$RELEASE_SHA on production host"
  remote_run "bash -s -- $(remote_quote "$REMOTE_ROOT") $(remote_quote "$REMOTE_BACKUP_DIR") $(remote_quote "$REMOTE_RELEASE_DIR") $(remote_quote "$remote_archive") $(remote_quote "$REMOTE_IMAGE_REPO") $(remote_quote "$RELEASE_SHA") $(remote_quote "$CONTAINER_UID") $(remote_quote "$REMOTE_DATA_DIRS")" <<'REMOTE_DEPLOY'
set -euo pipefail

remote_root="$1"
backup_dir="$2"
release_dir="$3"
artifact="$4"
image_repo="$5"
release_sha="$6"
container_uid="$7"
data_dirs="$8"
env_file="$remote_root/deploy/ec2/.env.production"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging="$release_dir/$timestamp"
backup="$backup_dir/home-energy-watch-$timestamp.tgz"

if [[ ! -f "$env_file" ]]; then
  echo "Missing production env file: $env_file" >&2
  exit 1
fi

mkdir -p "$remote_root" "$backup_dir" "$release_dir" "$staging"

if [[ -n "$(find "$remote_root" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  tar -C "$remote_root" \
    --exclude './.git' \
    --exclude './.venv' \
    --exclude './__pycache__' \
    --exclude './*.pyc' \
    --exclude './data' \
    --exclude './runtime' \
    --exclude './deploy/ec2/.env.production' \
    --exclude './.tmp-*' \
    -czf "$backup" .
fi

tar -xzf "$artifact" -C "$staging"
sudo rsync -a --delete \
  --exclude 'deploy/ec2/.env.production' \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$staging"/ "$remote_root"/

if [[ ! -f "$env_file" ]]; then
  echo "Production env file was not preserved: $env_file" >&2
  exit 1
fi

# The container runs as the unprivileged app user (uid/gid 10001). Host
# bind-mount directories must be writable by that uid. Idempotent.
for data_dir in $data_dirs; do
  sudo mkdir -p "$data_dir"
  sudo chown -R "$container_uid:$container_uid" "$data_dir"
done

cd "$remote_root"
# Build once, tagged by commit. The running service is not touched here.
docker build -t "$image_repo:$release_sha" .
rm -f "$artifact"
printf '%s\n' "Backup created: $backup"
printf '%s\n' "Built image: $image_repo:$release_sha"
REMOTE_DEPLOY

  if switch_remote_image && check_health; then
    printf '%s\n' "Deployed $REMOTE_IMAGE_REPO:$RELEASE_SHA as $REMOTE_IMAGE_REPO:current"
    return 0
  fi

  rollback_image_after_failed_deploy
}

switch_remote_image() {
  log "Promoting $REMOTE_IMAGE_REPO:$RELEASE_SHA to :current (running image retagged :previous) and restarting $REMOTE_SERVICE"
  remote_run "bash -s -- $(remote_quote "$REMOTE_ROOT") $(remote_quote "$REMOTE_SERVICE") $(remote_quote "$REMOTE_IMAGE_REPO") $(remote_quote "$RELEASE_SHA")" <<'REMOTE_SWITCH'
set -euo pipefail

remote_root="$1"
service="$2"
image_repo="$3"
release_sha="$4"

new_id="$(docker image inspect --format '{{.Id}}' "$image_repo:$release_sha")"
current_ref=""
if docker image inspect "$image_repo:current" >/dev/null 2>&1; then
  current_ref="$image_repo:current"
elif docker image inspect "$image_repo:latest" >/dev/null 2>&1; then
  # First tagged release: the legacy :latest image is what is running.
  current_ref="$image_repo:latest"
fi
if [[ -n "$current_ref" ]]; then
  current_id="$(docker image inspect --format '{{.Id}}' "$current_ref")"
  if [[ "$current_id" != "$new_id" ]]; then
    docker tag "$current_ref" "$image_repo:previous"
    printf '%s\n' "Retagged $current_ref as $image_repo:previous"
  fi
fi
docker tag "$image_repo:$release_sha" "$image_repo:current"
# Keep :latest pointing at the running release for older runbooks/scripts.
docker tag "$image_repo:$release_sha" "$image_repo:latest"

cd "$remote_root"
sudo install -m 0644 deploy/ec2/home-energy-watch-utility-sync.service /etc/systemd/system/home-energy-watch-utility-sync.service
sudo install -m 0644 deploy/ec2/home-energy-watch-utility-sync.timer /etc/systemd/system/home-energy-watch-utility-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now home-energy-watch-utility-sync.timer
sudo systemctl restart "$service"
sudo systemctl is-active --quiet "$service"
sudo systemctl is-active --quiet home-energy-watch-utility-sync.timer
REMOTE_SWITCH
}

rollback_image_after_failed_deploy() {
  printf '%s\n' "Error: release $RELEASE_SHA failed restart or health verification. Rolling back to $REMOTE_IMAGE_REPO:previous." >&2
  local rollback_ok=false
  if remote_run "bash -s -- $(remote_quote "$REMOTE_SERVICE") $(remote_quote "$REMOTE_IMAGE_REPO")" <<'REMOTE_IMAGE_ROLLBACK'
set -euo pipefail

service="$1"
image_repo="$2"

if ! docker image inspect "$image_repo:previous" >/dev/null 2>&1; then
  echo "No $image_repo:previous image exists on the host; cannot roll back automatically." >&2
  exit 1
fi
docker tag "$image_repo:previous" "$image_repo:current"
docker tag "$image_repo:previous" "$image_repo:latest"
sudo systemctl restart "$service"
sudo systemctl is-active --quiet "$service"
printf '%s\n' "Restored $image_repo:previous as $image_repo:current"
REMOTE_IMAGE_ROLLBACK
  then
    if check_health; then
      rollback_ok=true
    fi
  fi

  if [[ "$rollback_ok" == "true" ]]; then
    die "Deploy of $RELEASE_SHA FAILED and was rolled back to the previous image, which is healthy. The host source tree still contains the failed release; run 'scripts/deploy-production.sh rollback --confirm-production' to restore the source backup as well."
  fi
  die "Deploy of $RELEASE_SHA FAILED and the automatic image rollback did NOT restore health. Production is degraded: follow deploy/ec2/INCIDENT_RESPONSE.md now."
}

rollback_release() {
  require_remote_target
  log "Restoring production from backup"
  remote_run "bash -s -- $(remote_quote "$REMOTE_ROOT") $(remote_quote "$REMOTE_BACKUP_DIR") $(remote_quote "$REMOTE_RELEASE_DIR") $(remote_quote "$REMOTE_SERVICE") $(remote_quote "$requested_backup") $(remote_quote "$REMOTE_IMAGE_REPO")" <<'REMOTE_ROLLBACK'
set -euo pipefail

remote_root="$1"
backup_dir="$2"
release_dir="$3"
service="$4"
requested_backup="$5"
image_repo="$6"
env_file="$remote_root/deploy/ec2/.env.production"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
pre_rollback_backup="$backup_dir/home-energy-watch-pre-rollback-$timestamp.tgz"
staging="$release_dir/rollback-$timestamp"

if [[ ! -f "$env_file" ]]; then
  echo "Missing production env file: $env_file" >&2
  exit 1
fi

if [[ -n "$requested_backup" ]]; then
  case "$requested_backup" in
    /*) selected_backup="$requested_backup" ;;
    *) selected_backup="$backup_dir/$requested_backup" ;;
  esac
else
  selected_backup="$(ls -1t "$backup_dir"/home-energy-watch-[0-9]*.tgz 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${selected_backup:-}" || ! -f "$selected_backup" ]]; then
  echo "No rollback backup found." >&2
  exit 1
fi

mkdir -p "$backup_dir" "$release_dir" "$staging"
tar -C "$remote_root" \
  --exclude './.git' \
  --exclude './.venv' \
  --exclude './__pycache__' \
  --exclude './*.pyc' \
  --exclude './data' \
  --exclude './runtime' \
  --exclude './deploy/ec2/.env.production' \
  --exclude './.tmp-*' \
  -czf "$pre_rollback_backup" .

tar -xzf "$selected_backup" -C "$staging"
rsync -a --delete \
  --exclude 'deploy/ec2/.env.production' \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$staging"/ "$remote_root"/

if [[ ! -f "$env_file" ]]; then
  echo "Production env file was not preserved: $env_file" >&2
  exit 1
fi

cd "$remote_root"
# Rebuild the restored source and make it the image systemd runs.
rollback_tag="$image_repo:rollback-$timestamp"
docker build -t "$rollback_tag" .
docker tag "$rollback_tag" "$image_repo:current"
docker tag "$rollback_tag" "$image_repo:latest"
sudo install -m 0644 deploy/ec2/home-energy-watch-utility-sync.service /etc/systemd/system/home-energy-watch-utility-sync.service
sudo install -m 0644 deploy/ec2/home-energy-watch-utility-sync.timer /etc/systemd/system/home-energy-watch-utility-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now home-energy-watch-utility-sync.timer
sudo systemctl restart "$service"
sudo systemctl is-active --quiet "$service"
sudo systemctl is-active --quiet home-energy-watch-utility-sync.timer
printf '%s\n' "Rolled back from: $selected_backup"
printf '%s\n' "Pre-rollback backup created: $pre_rollback_backup"
REMOTE_ROLLBACK

  verify_health
}

run_check() {
  run_syntax_checks
  run_tests
  run_diff_check
  build_project
  if [[ -n "${PRODUCTION_SSH_TARGET:-}" ]]; then
    check_remote_prerequisites
  else
    log "Skipping remote prerequisite checks because PRODUCTION_SSH_TARGET is not set"
  fi
}

case "$command" in
  help)
    show_help
    ;;
  check)
    require_python
    require_command bash
    require_command curl
    require_command tar
    run_check
    ;;
  package)
    require_python
    require_command bash
    require_command tar
    require_release_commit
    package_release
    ;;
  deploy)
    require_production_confirmation
    require_python
    require_command bash
    require_command tar
    require_command ssh
    require_command scp
    require_command curl
    require_release_commit
    RELEASE_SHA="$(release_sha)"
    package_release
    snapshot_rds_before_deploy
    deploy_archive "$PACKAGE_ARCHIVE"
    ;;
  rollback)
    require_production_confirmation
    require_command bash
    require_command ssh
    require_command curl
    rollback_release
    ;;
  *)
    show_help >&2
    die "Unknown command: $command"
    ;;
esac
