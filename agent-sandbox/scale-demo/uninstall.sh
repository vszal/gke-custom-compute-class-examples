#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

NAMESPACE="agent-sandbox-demo"

echo "===================================================================="
echo "Uninstalling GKE Agent Sandbox - scale performance control tower"
echo "===================================================================="

if ! kubectl cluster-info &>/dev/null; then
  echo "Error: Cannot connect to Kubernetes cluster via kubectl. Check active context." >&2
  exit 1
fi
echo "Target Cluster Context: $(kubectl config current-context)"

if kubectl get namespace "${NAMESPACE}" &>/dev/null; then
  # Stop the control tower first, or its reconciler recreates what is deleted below.
  echo "Stopping the control tower..."
  kubectl scale deployment --all --replicas=0 -n "${NAMESPACE}" 2>/dev/null || true

  echo "Deleting claims, sessions, warm pools and buffers..."
  kubectl delete sandboxclaims --all -n "${NAMESPACE}" --ignore-not-found=true --wait=false || true
  kubectl delete sandboxwarmpools --all -n "${NAMESPACE}" --ignore-not-found=true --wait=false || true
  kubectl delete sandboxes --all -n "${NAMESPACE}" --ignore-not-found=true --wait=false || true
  if kubectl get crd podsnapshotmanualtriggers.podsnapshot.gke.io &>/dev/null; then
    kubectl delete podsnapshotmanualtriggers,podsnapshotpolicies --all -n "${NAMESPACE}" \
      --ignore-not-found=true || true
  fi
  kubectl delete capacitybuffers --all -n "${NAMESPACE}" --ignore-not-found=true || true

  echo "Deleting namespace ${NAMESPACE}..."
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found=true --timeout=120s || true
fi

# Cluster-scoped objects from both modes.
echo "Deleting cluster-scoped objects (ComputeClasses, CapacityQuotas, RBAC, storage config)..."
for f in manifests/02-capacity-quotas.yaml snapshots/02-capacity-quotas.yaml \
         manifests/01-compute-class.yaml snapshots/01-compute-class.yaml; do
  kubectl delete -f "${f}" --ignore-not-found=true || true
done
kubectl delete clusterrolebinding sbx-demo-dashboard-binding --ignore-not-found=true || true
kubectl delete clusterrole sbx-demo-dashboard-role --ignore-not-found=true || true
if kubectl get crd podsnapshotstorageconfigs.podsnapshot.gke.io &>/dev/null; then
  kubectl delete podsnapshotstorageconfig sbx-demo-snapshots --ignore-not-found=true || true
fi

echo "===================================================================="
echo "Teardown complete! The Pod Snapshots bucket, if you made one, is left in"
echo "place; delete it with: gcloud storage rm -r gs://BUCKET"
echo "===================================================================="
