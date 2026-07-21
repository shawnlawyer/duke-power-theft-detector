#!/bin/sh

set -eu

IMAGE_TAG="${1:-home-energy-watch:security-scan}"
TRIVY_IMAGE="${TRIVY_IMAGE:-aquasec/trivy@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e}"
PYTHON_AUDIT_IMAGE="${PYTHON_AUDIT_IMAGE:-python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93}"
ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
TRIVY_CACHE_DIR="${TRIVY_CACHE_DIR:-/tmp/home-energy-watch-trivy-cache}"

mkdir -p "$TRIVY_CACHE_DIR"

docker build -t "$IMAGE_TAG" .
docker run --rm "$IMAGE_TAG" python -m pytest
docker run --rm \
  -v "$ROOT_DIR:/workspace:ro" \
  -w /workspace \
  "$PYTHON_AUDIT_IMAGE" sh -c \
  "python -m pip install --disable-pip-version-check --no-cache-dir pip-audit==2.10.0 >/tmp/pip-audit-install.log && python -m pip_audit -r requirements.lock --disable-pip --no-deps --progress-spinner off"
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$TRIVY_CACHE_DIR:/root/.cache/" \
  "$TRIVY_IMAGE" image \
  --exit-code 1 \
  --severity HIGH,CRITICAL \
  --ignore-unfixed \
  --skip-version-check \
  "$IMAGE_TAG"
