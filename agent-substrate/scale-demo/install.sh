#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

usage() {
  cat <<'EOF'
Usage: ./install.sh [--mode=pause|substrate] [--bucket=NAME]

  --mode=pause      (default) Pause-pod workers and stub ate.dev CRDs. Shows the
                    ComputeClass / CapacityBuffer / CapacityQuota scaling story on any
                    cluster, with nothing else installed.
  --mode=substrate  Real Agent Substrate WorkerPools and ActorTemplates, one per machine
                    family. Requires Agent Substrate already installed on the cluster
                    (see README.md, "Substrate mode").
  --bucket=NAME     Substrate mode only: the GCS bucket Substrate stores snapshots in
                    (the one its installer created). Or set SUBSTRATE_BUCKET.
EOF
}

MODE="pause"
BUCKET="${SUBSTRATE_BUCKET:-}"
for arg in "$@"; do
  case "${arg}" in
    --mode=*) MODE="${arg#--mode=}" ;;
    --bucket=*) BUCKET="${arg#--bucket=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: ${arg}" >&2; usage >&2; exit 1 ;;
  esac
done
if [[ "${MODE}" != "pause" && "${MODE}" != "substrate" ]]; then
  echo "Error: --mode must be pause or substrate (got '${MODE}')." >&2
  exit 1
fi

NAMESPACE="agent-substrate-demo"
MICROVM_FAMILIES=(c4 n4 c3)
GVISOR_FAMILIES=(c4 c4d n4 n4d c3 c3d)

echo "=================================================================="
echo "Installing GKE Agent Substrate - scale performance control tower"
echo "Mode: ${MODE}"
echo "=================================================================="

# Verify kubectl connectivity
if ! kubectl cluster-info &>/dev/null; then
  echo "Error: Cannot connect to Kubernetes cluster via kubectl. Check active context." >&2
  exit 1
fi

CONTEXT="$(kubectl config current-context)"
echo "Target Cluster Context: ${CONTEXT}"

# The pause-mode stub CRDs share names with the real Agent Substrate CRDs. Applying them
# over a real install would replace Substrate's schema, so detect which one is present.
real_substrate_crds() {
  kubectl get crd workerpools.ate.dev \
    -o jsonpath='{.spec.versions[*].schema.openAPIV3Schema.properties.spec.properties}' 2>/dev/null \
    | grep -q '"sandboxClass"'
}

install_pause() {
  if real_substrate_crds; then
    echo "Error: this cluster runs real Agent Substrate (workerpools.ate.dev has sandboxClass)." >&2
    echo "Pause mode would overwrite its CRDs with stubs. Use --mode=substrate instead." >&2
    exit 1
  fi

  echo "Applying namespace and structural CRDs..."
  kubectl apply -f manifests/00-namespace-and-crds.yaml
  kubectl wait --for condition=established --timeout=30s crd/workerpools.ate.dev crd/actortemplates.ate.dev

  echo "Applying remaining Kubernetes manifests via Kustomize..."
  kubectl apply -k .
}

# Pins host/path:tag to host/path:tag@sha256:... with an anonymous HEAD against the
# registry's v2 API. Substrate rejects ActorTemplate images that are not pinned (a
# changed image would invalidate its snapshots).
pin_digest() {
  local image="$1"
  if [[ "${image}" == *@sha256:* ]]; then
    echo "${image}"; return
  fi
  local host="${image%%/*}" rest="${image#*/}"
  local repo="${rest%:*}" tag="${rest##*:}"
  local digest
  digest="$(curl -fsSI \
    -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json" \
    "https://${host}/v2/${repo}/manifests/${tag}" \
    | awk 'tolower($1)=="docker-content-digest:"{print $2}' | tr -d '\r')"
  if [[ -z "${digest}" ]]; then
    echo "Error: could not resolve a digest for ${image}; set the *_IMAGE variable to a digest-pinned ref." >&2
    return 1
  fi
  echo "${image}@${digest}"
}

# Substrate persists every golden snapshot to this bucket. A missing bucket makes the
# golden build fail with "GoldenActorCrashed", so create it (in the cluster's project and
# region, with Substrate's service accounts granted access) when it does not exist.
# An existing bucket is used as is.
ensure_bucket() {
  if gcloud storage buckets describe "gs://${BUCKET}" &>/dev/null; then
    echo "Snapshot bucket gs://${BUCKET} exists; using it as is."
    return
  fi
  # GKE kubeconfig contexts are named gke_<project>_<location>_<cluster>.
  local ctx project="" location="" number pool sa role
  ctx="$(kubectl config current-context)"
  if [[ "${ctx}" == gke_*_*_* ]]; then
    IFS=_ read -r _ project location _ <<<"${ctx}"
  fi
  project="${BUCKET_PROJECT:-${project:-$(gcloud config get-value project 2>/dev/null)}}"
  location="${BUCKET_LOCATION:-${location}}"
  if [[ -z "${project}" || -z "${location}" ]]; then
    echo "Error: gs://${BUCKET} does not exist and the project/region could not be derived from the kubectl context." >&2
    echo "Create it yourself, or set BUCKET_PROJECT and BUCKET_LOCATION (for example us-central1) and rerun." >&2
    exit 1
  fi
  # A zonal cluster (us-central1-a) needs the region for the bucket.
  [[ "${location}" =~ ^[a-z]+-[a-z]+[0-9]+-[a-z]$ ]] && location="${location%-*}"
  echo "Creating snapshot bucket gs://${BUCKET} in ${project} (${location})..."
  gcloud storage buckets create "gs://${BUCKET}" --project="${project}" \
    --location="${location}" --uniform-bucket-level-access || {
    echo "Error: could not create gs://${BUCKET} (the name may be taken, or you lack storage.buckets.create on ${project})." >&2
    exit 1
  }
  number="$(gcloud projects describe "${project}" --format='value(projectNumber)')"
  pool="projects/${number}/locations/global/workloadIdentityPools/${project}.svc.id.goog"
  for sa in ate-api-server atelet; do
    for role in roles/storage.objectAdmin roles/storage.bucketViewer; do
      gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --role="${role}" \
        --member="principal://iam.googleapis.com/${pool}/subject/ns/ate-system/sa/${sa}" >/dev/null
    done
  done
}

install_substrate() {
  echo "Preflight checks..."
  if ! kubectl ate --help &>/dev/null; then
    echo "Error: the kubectl-ate plugin is not on PATH. It is a separate binary you build from source:" >&2
    echo "  git clone --branch <upstream tag matching ate-system, e.g. v0.4.0 for v0.4.0-gke.0> https://github.com/agent-substrate/substrate.git" >&2
    echo "  cd substrate && go install ./cmd/kubectl-ate    # needs Go 1.27+; installs to \$(go env GOPATH)/bin" >&2
    echo "Then add that bin directory to PATH. (go install ...@latest does not work: the module uses replace directives.)" >&2
    exit 1
  fi
  if ! real_substrate_crds; then
    echo "Error: Agent Substrate CRDs not found (or only the pause-mode stubs are present)." >&2
    echo "Install Agent Substrate first, or run uninstall.sh to remove the stubs." >&2
    exit 1
  fi
  if ! kubectl get namespace ate-system &>/dev/null; then
    echo "Error: namespace ate-system not found; Agent Substrate does not look installed." >&2
    exit 1
  fi
  if [[ -z "${BUCKET}" ]]; then
    echo "Error: pass --bucket=NAME (or set SUBSTRATE_BUCKET) -- the Substrate snapshot bucket." >&2
    exit 1
  fi

  ensure_bucket

  # atelet only runs on nodes carrying ate.dev/substrate-version=<version>. Read the
  # version off its DaemonSet so the ComputeClass stamps the matching label.
  local version
  version="$(kubectl get daemonset -n ate-system \
    -o jsonpath='{.items[*].spec.template.spec.nodeSelector.ate\.dev/substrate-version}' | awk '{print $1}')"
  if [[ -z "${version}" ]]; then
    echo "Error: could not read ate.dev/substrate-version from the atelet DaemonSet in ate-system." >&2
    exit 1
  fi
  local registry="${SUBSTRATE_REGISTRY:-us-docker.pkg.dev/gke-substrate-release/substrate}"
  local microvm_image gvisor_image counter_image
  microvm_image="$(pin_digest "${MICROVM_WORKER_IMAGE:-${registry}/ateom-microvm:${version}}")"
  gvisor_image="$(pin_digest "${GVISOR_WORKER_IMAGE:-${registry}/ateom-gvisor:${version}}")"
  counter_image="$(pin_digest "${COUNTER_IMAGE:-${registry}/counter:${version}}")"
  echo "Substrate version: ${version}"
  echo "Counter image: ${counter_image}"

  local runtimes=(gvisor)
  if kubectl get sandboxconfigs microvm &>/dev/null; then
    runtimes+=(microvm)
  else
    echo "Note: SandboxConfig 'microvm' not found; creating gVisor templates only."
    echo "      Run Substrate's hack/install-microvm-deps.sh to enable microVM."
  fi

  echo "Applying ComputeClasses, CapacityQuotas, buffers and WorkerPools..."
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  local f
  for f in substrate/01-compute-class.yaml substrate/02-capacity-quotas.yaml \
           substrate/03-buffers.yaml substrate/04-worker-pools.yaml; do
    sed -e "s|__SUBSTRATE_VERSION__|${version}|g" \
        -e "s|__MICROVM_WORKER_IMAGE__|${microvm_image}|g" \
        -e "s|__GVISOR_WORKER_IMAGE__|${gvisor_image}|g" "${f}" \
      | kubectl apply -f -
  done

  echo "Creating atespace and per-family ActorTemplates..."
  # Capture stdout only; stderr (auth, connectivity, plugin/server version mismatch) must
  # reach the user instead of reading as "nothing exists" and failing later with AlreadyExists.
  local atespaces existing
  atespaces="$(kubectl ate get atespaces)" || {
    echo "Error: 'kubectl ate get atespaces' failed (see above). A protobuf 'unknown field' error means the plugin version does not match the server's; see the README." >&2
    exit 1
  }
  if ! awk 'NR>1{print $1}' <<<"${atespaces}" | grep -qx "${NAMESPACE}"; then
    kubectl ate create atespace "${NAMESPACE}"
  fi
  existing="$(kubectl ate get actor-templates -a "${NAMESPACE}")" || {
    echo "Error: 'kubectl ate get actor-templates -a ${NAMESPACE}' failed (see above)." >&2
    exit 1
  }
  existing="$(awk 'NR>1{print $2}' <<<"${existing}")"
  local rt fam sclass sconfig
  local -a families
  for rt in "${runtimes[@]}"; do
    if [[ "${rt}" == "microvm" ]]; then
      families=("${MICROVM_FAMILIES[@]}"); sclass=SANDBOX_CLASS_MICROVM; sconfig=microvm
    else
      families=("${GVISOR_FAMILIES[@]}"); sclass=SANDBOX_CLASS_GVISOR; sconfig=gvisor-default
    fi
    for fam in "${families[@]}"; do
      if grep -qx "${rt}-${fam}" <<<"${existing}"; then
        echo "  actor-template ${rt}-${fam} exists"
        continue
      fi
      sed -e "s|\${RUNTIME}|${rt}|g" -e "s|\${FAMILY}|${fam}|g" -e "s|\${BUCKET}|${BUCKET}|g" \
          -e "s|\${COUNTER_IMAGE}|${counter_image}|g" -e "s|\${SANDBOX_CLASS}|${sclass}|g" \
          -e "s|\${SANDBOX_CONFIG}|${sconfig}|g" substrate/actor-template.yaml.tmpl \
        | kubectl ate create actor-template -f -
    done
  done

  # kubectl apply -k would also apply the stub CRDs and pause-pod pools (00, 04), so
  # build the dashboard's ConfigMap directly under the unhashed name 05 references.
  echo "Deploying the control tower in substrate mode..."
  kubectl create configmap ccc-agents-dashboard-app -n "${NAMESPACE}" \
    --from-file=server.py=app/server.py \
    --from-file=index.html=app/static/index.html \
    --from-file=app.js=app/static/app.js \
    --from-file=guide.js=app/static/guide.js \
    --from-file=styles.css=app/static/styles.css \
    --from-file=requirements.txt=app/requirements.txt \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f manifests/05-dashboard.yaml
  kubectl set env deployment/ccc-agents-dashboard -n "${NAMESPACE}" DEMO_MODE=substrate
}

if [[ "${MODE}" == "substrate" ]]; then
  install_substrate
else
  install_pause
fi

echo "Waiting for dashboard deployment rollout..."
kubectl rollout status deployment/ccc-agents-dashboard -n "${NAMESPACE}" --timeout=420s || {
  echo "Warning: Dashboard deployment rollout pending or timed out. Check pod status with: kubectl get pods -n ${NAMESPACE}"
}

echo ""
echo "=================================================================="
echo "Installation complete!"
echo "To access the web dashboard locally:"
echo "  kubectl port-forward svc/ccc-agents-dashboard 8881:8881 -n ${NAMESPACE}"
echo "Then open: http://localhost:8881"
if [[ "${MODE}" == "substrate" ]]; then
  echo ""
  echo "Substrate mode: golden snapshots build on the first scale-up. Watch them with:"
  echo "  kubectl ate get actor-templates -a ${NAMESPACE} -w"
  echo "Create actors once GOLDEN TAG is filled in."
fi
echo "=================================================================="
