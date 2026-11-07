#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn matchlab.api:app --host "${MATCHLAB_HOST:-127.0.0.1}" --port "${MATCHLAB_PORT:-8080}"
