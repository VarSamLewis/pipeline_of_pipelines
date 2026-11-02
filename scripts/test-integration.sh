#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

ci_image="${PYTHON_CI_IMAGE:-python-ci:latest}"
if ! docker image inspect "$ci_image" >/dev/null 2>&1; then
  docker build -f Dockerfile.ci -t "$ci_image" "$repo_root"
fi

docker compose up -d --wait postgres

if [[ "$(docker compose exec -T postgres psql -U postgres -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname = 'pipeline_test'")" != "1" ]]; then
  docker compose exec -T postgres psql -U postgres -d postgres \
    -c 'CREATE DATABASE pipeline_test'
fi

docker run --rm --network host \
  -e DATABASE_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:5432/pipeline_test' \
  -e AUTH_BYPASS_LOCAL=true \
  -e HOME=/tmp \
  -e UV_PROJECT_ENVIRONMENT=/tmp/pipeline-ci-venv \
  -v "$repo_root:/workspace" \
  -w /workspace \
  "$ci_image" \
  -c 'if command -v python3.13 >/dev/null 2>&1; then
        python="$(command -v python3.13)"
      else
        candidates=(/nix/store/*-python3-3.14.*/bin/python3.14)
        if [[ ! -x "${candidates[0]}" ]]; then
          echo "CI image needs a working Python 3.13 or Nix Python 3.14" >&2
          exit 1
        fi
        python="${candidates[0]}"
      fi
      uv sync --frozen --extra dev --python "$python" &&
      uv run --frozen --python "$python" pytest -q'
