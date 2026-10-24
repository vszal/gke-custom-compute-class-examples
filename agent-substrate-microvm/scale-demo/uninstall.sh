#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "===================================================================="
echo "Uninstalling GKE Agent Substrate - scale performance control tower"
echo "===================================================================="

# Verify kubectl connectivity
if ! kubectl cluster-info &>/dev/null; then
  echo "Error: Cannot connect to Kubernetes cluster via kubectl. Check active context." >&2
  exit 1
fi

CONTEXT="$(kubectl config current-context)"
echo "Target Cluster Context: ${CONTEXT}"

echo "Scaling down active workloads and CapacityBuffers to zero..."
for ns in agent-substrate-demo ccc-agents; do
  if kubectl get namespace "${ns}" &>/dev/null; then
    kubectl scale deployment --all --replicas=0 -n "${ns}" 2>/dev/null || true
    for buf in $(kubectl get capacitybuffers -n "${ns}" -o name 2>/dev/null || true); do
      kubectl patch "${buf}" -n "${ns}" --type=merge -p '{"spec":{"replicas":0}}' 2>/dev/null || true
    done
    kubectl delete capacitybuffers --all -n "${ns}" --ignore-not-found=true || true
  fi
done

echo "Deleting demo namespaces (agent-substrate-demo, ccc-agents)..."
kubectl delete namespace agent-substrate-demo ccc-agents --ignore-not-found=true --timeout=60s || true

echo "Deleting cluster-scoped CapacityQuotas, ComputeClasses, and CRDs..."
kubectl delete -f manifests/02-capacity-quotas.yaml --ignore-not-found=true || true
kubectl delete -f manifests/01-compute-class.yaml --ignore-not-found=true || true
kubectl delete -f manifests/00-namespace-and-crds.yaml --ignore-not-found=true || true

echo "===================================================================="
echo "Teardown complete!"
echo "===================================================================="
