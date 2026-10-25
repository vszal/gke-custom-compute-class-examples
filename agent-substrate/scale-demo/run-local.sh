#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8881}"

echo "=================================================================="
echo "Running GKE Agent Substrate - scale performance control tower"
echo "=================================================================="

if ! command -v python3 &>/dev/null; then
  echo "Error: python3 not found." >&2
  exit 1
fi

# Verify kubectl context
if ! kubectl cluster-info &>/dev/null; then
  echo "Warning: kubectl cluster-info failed. Running in mock/offline simulation mode if cluster is unreachable." >&2
fi

if [[ ! -d ".venv" ]]; then
  echo "Creating Python virtual environment (.venv)..."
  python3 -m venv .venv
fi

echo "Installing/verifying Python dependencies in .venv..."
.venv/bin/pip install -q -r app/requirements.txt

echo "Starting FastAPI server on http://${HOST}:${PORT}..."
PYTHONPATH="${SCRIPT_DIR}" exec .venv/bin/python app/server.py --host "${HOST}" --port "${PORT}" "$@"
