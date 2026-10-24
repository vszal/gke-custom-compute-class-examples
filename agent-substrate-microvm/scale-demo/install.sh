#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "=================================================================="
echo "Installing GKE Agent Substrate - scale performance control tower"
echo "=================================================================="

# Verify kubectl connectivity
if ! kubectl cluster-info &>/dev/null; then
  echo "Error: Cannot connect to Kubernetes cluster via kubectl. Check active context." >&2
  exit 1
fi

CONTEXT="$(kubectl config current-context)"
echo "Target Cluster Context: ${CONTEXT}"

echo "Applying namespace and structural CRDs..."
kubectl apply -f manifests/00-namespace-and-crds.yaml
kubectl wait --for condition=established --timeout=30s crd/workerpools.ate.dev crd/actortemplates.ate.dev

echo "Applying remaining Kubernetes manifests via Kustomize..."
kubectl apply -k .

echo "Waiting for dashboard deployment rollout..."
kubectl rollout status deployment/ccc-agents-dashboard -n agent-substrate-demo --timeout=180s || {
  echo "Warning: Dashboard deployment rollout pending or timed out. Check pod status with: kubectl get pods -n agent-substrate-demo"
}

echo ""
echo "=================================================================="
echo "Installation complete!"
echo "To access the web dashboard locally:"
echo "  kubectl port-forward svc/ccc-agents-dashboard 8881:8881 -n agent-substrate-demo"
echo "Then open: http://localhost:8881"
echo "=================================================================="
