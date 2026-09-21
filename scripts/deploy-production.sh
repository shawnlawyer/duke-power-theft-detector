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
PACKAGE_ARCHIVE=""

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
  deploy      Package, copy the archive to the production EC2 host, back up the
              current release, replace only application source, restart the
              home-energy-watch systemd service, and verify /health.
  rollback    Restore the latest backup, or the backup named with --backup,
              restart the home-energy-watch service, and verify /health.
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

verify_health() {
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
  die "Health check failed after $HEALTH_RETRIES attempts: $HEALTH_URL"
}

deploy_archive() {
  local archive="$1"
  local remote_archive="/tmp/$(basename "$archive")"
  require_remote_target
  log "Copying release archive to production host"
  remote_copy "$archive" "$remote_archive"

  log "Installing release on production host"
  remote_run "bash -s -- $(remote_quote "$REMOTE_ROOT") $(remote_quote "$REMOTE_BACKUP_DIR") $(remote_quote "$REMOTE_RELEASE_DIR") $(remote_quote "$REMOTE_SERVICE") $(remote_quote "$remote_archive")" <<'REMOTE_DEPLOY'
set -euo pipefail

remote_root="$1"
backup_dir="$2"
release_dir="$3"
service="$4"
artifact="$5"
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

cd "$remote_root"
docker build -t home-energy-watch:latest .
sudo install -m 0644 deploy/ec2/home-energy-watch-utility-sync.service /etc/systemd/system/home-energy-watch-utility-sync.service
sudo install -m 0644 deploy/ec2/home-energy-watch-utility-sync.timer /etc/systemd/system/home-energy-watch-utility-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now home-energy-watch-utility-sync.timer
sudo systemctl restart "$service"
sudo systemctl is-active --quiet "$service"
sudo systemctl is-active --quiet home-energy-watch-utility-sync.timer
rm -f "$artifact"
printf '%s\n' "Backup created: $backup"
REMOTE_DEPLOY

  verify_health
}

rollback_release() {
  require_remote_target
  log "Restoring production from backup"
  remote_run "bash -s -- $(remote_quote "$REMOTE_ROOT") $(remote_quote "$REMOTE_BACKUP_DIR") $(remote_quote "$REMOTE_RELEASE_DIR") $(remote_quote "$REMOTE_SERVICE") $(remote_quote "$requested_backup")" <<'REMOTE_ROLLBACK'
set -euo pipefail

remote_root="$1"
backup_dir="$2"
release_dir="$3"
service="$4"
requested_backup="$5"
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
docker build -t home-energy-watch:latest .
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
    package_release
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
