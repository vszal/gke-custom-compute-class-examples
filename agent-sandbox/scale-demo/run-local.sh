#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8881}"

echo "=================================================================="
echo "Running GKE Agent Sandbox - scale performance control tower (DEMO_MODE=${DEMO_MODE:-stateless})"
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

# Both towers reconcile the same claims and warm pools toward their own targets, so a
# local live tower pauses the in-cluster dashboard instead of fighting it.
if [[ " $* " != *" --mock "* ]] && kubectl get deploy sbx-demo-dashboard -n agent-sandbox-demo &>/dev/null; then
  if [[ "$(kubectl get deploy sbx-demo-dashboard -n agent-sandbox-demo -o jsonpath='{.spec.replicas}')" != "0" ]]; then
    echo "Pausing the in-cluster dashboard so it does not reconcile against this tower."
    echo "  Restore it with: kubectl scale deploy/sbx-demo-dashboard -n agent-sandbox-demo --replicas=1"
    kubectl scale deploy/sbx-demo-dashboard -n agent-sandbox-demo --replicas=0 >/dev/null
  fi
fi

echo "Starting FastAPI server on http://${HOST}:${PORT}..."
PYTHONPATH="${SCRIPT_DIR}" exec .venv/bin/python app/server.py --host "${HOST}" --port "${PORT}" "$@"
