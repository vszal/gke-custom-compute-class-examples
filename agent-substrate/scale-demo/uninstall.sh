#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

NAMESPACE="agent-substrate-demo"

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

# Same check as install.sh: real Agent Substrate CRDs carry spec.sandboxClass.
real_substrate_crds() {
  kubectl get crd workerpools.ate.dev \
    -o jsonpath='{.spec.versions[*].schema.openAPIV3Schema.properties.spec.properties}' 2>/dev/null \
    | grep -q '"sandboxClass"'
}

if real_substrate_crds; then
  SUBSTRATE=1
else
  SUBSTRATE=0
fi

# kubectl ate list that warns on failure (e.g. plugin/server version mismatch) instead of
# looking like an empty list and silently skipping the cleanup.
ate_list() {
  local out
  if ! out="$(kubectl ate get "$@" 2>&1)"; then
    echo "WARNING: 'kubectl ate get $*' failed: ${out}" >&2
    return 0
  fi
  printf '%s\n' "${out}"
}

# Substrate mode: actors must go before their workers. Deleting the namespace under a
# running actor leaves it CRASHED, so suspend, delete actors, tags, templates, then the
# (now empty) atespace, and only then remove the WorkerPools.
if [[ "${SUBSTRATE}" == 1 ]] && kubectl ate --help &>/dev/null \
   && ate_list atespaces | awk 'NR>1{print $1}' | grep -qx "${NAMESPACE}"; then
  echo "Removing Substrate actors, tags, templates and the ${NAMESPACE} atespace..."
  for actor in $(ate_list actors -a "${NAMESPACE}" | awk 'NR>1 && $4=="ACTOR_STATE_RUNNING"{print $2}'); do
    kubectl ate suspend actor "${actor}" -a "${NAMESPACE}" || true
  done
  for actor in $(ate_list actors -a "${NAMESPACE}" | awk 'NR>1{print $2}'); do
    kubectl ate delete actor "${actor}" -a "${NAMESPACE}" || true
  done
  for tag in $(ate_list tags -a "${NAMESPACE}" | awk 'NR>1{print $2}'); do
    kubectl ate delete tag "${tag}" -a "${NAMESPACE}" || true
  done
  for tmpl in $(ate_list actor-templates -a "${NAMESPACE}" | awk 'NR>1{print $2}'); do
    kubectl ate delete actor-template "${tmpl}" -a "${NAMESPACE}" || true
  done
  kubectl ate delete atespace "${NAMESPACE}" || true
fi

echo "Scaling down active workloads and CapacityBuffers to zero..."
for ns in "${NAMESPACE}" ccc-agents; do
  if kubectl get namespace "${ns}" &>/dev/null; then
    kubectl scale deployment --all --replicas=0 -n "${ns}" 2>/dev/null || true
    for buf in $(kubectl get capacitybuffers -n "${ns}" -o name 2>/dev/null || true); do
      kubectl patch "${buf}" -n "${ns}" --type=merge -p '{"spec":{"replicas":0}}' 2>/dev/null || true
    done
    kubectl delete capacitybuffers --all -n "${ns}" --ignore-not-found=true || true
    if [[ "${SUBSTRATE}" == 1 ]]; then
      kubectl delete workerpools --all -n "${ns}" --ignore-not-found=true || true
    fi
  fi
done

echo "Deleting demo namespaces (${NAMESPACE}, ccc-agents)..."
kubectl delete namespace "${NAMESPACE}" ccc-agents --ignore-not-found=true --timeout=60s || true

echo "Deleting cluster-scoped CapacityQuotas and ComputeClasses..."
if [[ "${SUBSTRATE}" == 1 ]]; then
  kubectl delete -f substrate/02-capacity-quotas.yaml --ignore-not-found=true || true
  kubectl delete -f substrate/01-compute-class.yaml --ignore-not-found=true || true
else
  kubectl delete -f manifests/02-capacity-quotas.yaml --ignore-not-found=true || true
  kubectl delete -f manifests/01-compute-class.yaml --ignore-not-found=true || true
fi

# Never delete the CRDs when they belong to a real Agent Substrate install: that would
# delete every WorkerPool on the cluster, not just this demo's.
if [[ "${SUBSTRATE}" == 1 ]]; then
  echo "Leaving ate.dev CRDs in place (they belong to the Agent Substrate install)."
else
  echo "Deleting pause-mode stub CRDs..."
  kubectl delete -f manifests/00-namespace-and-crds.yaml --ignore-not-found=true || true
fi

echo "===================================================================="
echo "Teardown complete!"
echo "===================================================================="
