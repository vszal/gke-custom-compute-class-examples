#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--mode=stateless|snapshots|snapshots-rootfs] [--bucket=NAME]

  --mode=stateless  (default) One SandboxClaim per agent, served from a SandboxWarmPool,
                    on microVM (Kata) or gVisor across a five-family ComputeClass ladder.
  --mode=snapshots  One gVisor session Sandbox per agent that can be snapshotted,
                    suspended and resumed with Pod Snapshots. One machine family (n4),
                    fallback by size only. Needs Pod Snapshots enabled on the cluster and
                    a bucket from scripts/setup-snapshot-bucket.sh.
  --mode=snapshots-rootfs
                    Same session Sandboxes, but with rootfs-only Pod Snapshots: only files
                    on the root filesystem survive (not /tmp, no memory or processes), and
                    the restore works across machine families, so the wide gVisor class
                    (c3 / n4 / c4 / c2 / n2) from manifests/ is reused. Same prerequisites.
  --bucket=NAME     Snapshot modes only: the Pod Snapshots bucket. Or set SNAPSHOT_BUCKET.

All modes need Agent Sandbox enabled on the cluster. See README.md, "Prerequisites".
USAGE
}

MODE="stateless"
BUCKET="${SNAPSHOT_BUCKET:-}"
for arg in "$@"; do
  case "${arg}" in
    --mode=*) MODE="${arg#--mode=}" ;;
    --bucket=*) BUCKET="${arg#--bucket=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: ${arg}" >&2; usage >&2; exit 1 ;;
  esac
done
if [[ "${MODE}" != "stateless" && "${MODE}" != "snapshots" && "${MODE}" != "snapshots-rootfs" ]]; then
  echo "Error: --mode must be stateless, snapshots or snapshots-rootfs (got '${MODE}')." >&2
  exit 1
fi

NAMESPACE="agent-sandbox-demo"

echo "=================================================================="
echo "Installing GKE Agent Sandbox - scale performance control tower"
echo "Mode: ${MODE}"
echo "=================================================================="

if ! kubectl cluster-info &>/dev/null; then
  echo "Error: Cannot connect to Kubernetes cluster via kubectl. Check active context." >&2
  exit 1
fi
echo "Target Cluster Context: $(kubectl config current-context)"

require_crd() {
  if ! kubectl get crd "$1" &>/dev/null; then
    echo "Error: CRD $1 not found. $2" >&2
    exit 1
  fi
}

echo "Preflight checks..."
require_crd sandboxes.agents.x-k8s.io \
  "Enable Agent Sandbox: gcloud container clusters update CLUSTER --enable-agent-sandbox"
require_crd sandboxclaims.extensions.agents.x-k8s.io "Agent Sandbox extensions are missing."
require_crd capacitybuffers.autoscaling.x-k8s.io \
  "CapacityBuffers need GKE 1.35.2-gke.1842000 or later."
require_crd capacityquotas.autoscaling.x-k8s.io \
  "CapacityQuotas need GKE 1.36.2-gke.2064000 or later."

if [[ "${MODE}" == "stateless" ]]; then
  if ! kubectl get runtimeclass microvm &>/dev/null; then
    echo "Note: RuntimeClass microvm not found. The microVM runtime needs GKE"
    echo "      1.37.0-gke.4713000+; gVisor works without it."
  fi
  echo "Applying manifests and the control tower via Kustomize..."
  kubectl apply -k .
else
  require_crd podsnapshotstorageconfigs.podsnapshot.gke.io \
    "Enable Pod Snapshots: gcloud container clusters update CLUSTER --enable-pod-snapshots"
  if [[ -z "${BUCKET}" ]]; then
    echo "Error: pass --bucket=NAME (or set SNAPSHOT_BUCKET). Create it with" >&2
    echo "       scripts/setup-snapshot-bucket.sh NAME" >&2
    exit 1
  fi

  if [[ "${MODE}" == "snapshots" ]]; then
    echo "Applying namespace, snapshot ComputeClass, quota, buffers and session template..."
    kubectl apply -f manifests/00-namespace.yaml
    kubectl apply -f snapshots/01-compute-class.yaml
    kubectl apply -f snapshots/02-capacity-quotas.yaml
    kubectl apply -f snapshots/03-buffers.yaml
    sed "s|__BUCKET__|${BUCKET}|g" snapshots/04-sessions.yaml | kubectl apply -f -
  else
    # Rootfs-only restores across families, so the stateless wide gVisor class, quotas and
    # buffers (manifests/00-03) serve the sessions unchanged.
    echo "Applying namespace, wide gVisor ComputeClass, quotas, buffers and rootfs session template..."
    kubectl apply -f manifests/00-namespace.yaml
    kubectl apply -f manifests/01-compute-class.yaml
    kubectl apply -f manifests/02-capacity-quotas.yaml
    # The buffers default to the microVM templates; point them at gVisor up front so the
    # first standby nodes are built on the right class instead of being replaced later.
    sed 's|^    name: agent-microvm-buffer-|    name: agent-gvisor-buffer-|' manifests/03-buffers.yaml | kubectl apply -f -
    sed "s|__BUCKET__|${BUCKET}|g" snapshots-rootfs/04-sessions.yaml | kubectl apply -f -
  fi

  # kubectl apply -k would also apply the stateless manifests, so build the dashboard's
  # ConfigMap directly under the unhashed name 05 references.
  echo "Deploying the control tower in ${MODE} mode..."
  kubectl create configmap sbx-demo-dashboard-app -n "${NAMESPACE}" \
    --from-file=server.py=app/server.py \
    --from-file=index.html=app/static/index.html \
    --from-file=app.js=app/static/app.js \
    --from-file=guide.js=app/static/guide.js \
    --from-file=styles.css=app/static/styles.css \
    --from-file=requirements.txt=app/requirements.txt \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f manifests/05-dashboard.yaml
fi
kubectl set env deployment/sbx-demo-dashboard -n "${NAMESPACE}" DEMO_MODE="${MODE}"

echo "Waiting for dashboard deployment rollout..."
kubectl rollout status deployment/sbx-demo-dashboard -n "${NAMESPACE}" --timeout=420s || {
  echo "Warning: Dashboard deployment rollout pending or timed out. Check pod status with: kubectl get pods -n ${NAMESPACE}"
}

echo ""
echo "=================================================================="
echo "Installation complete!"
echo "To access the web dashboard locally:"
echo "  kubectl port-forward svc/sbx-demo-dashboard 8881:8881 -n ${NAMESPACE}"
echo "Then open: http://localhost:8881"
echo "=================================================================="
