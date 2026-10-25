#!/usr/bin/env bash
# ==============================================================================
# substrate-actor-load.sh
# Substrate mode only: measures actor latency per machine family.
# For each family's ActorTemplate (<runtime>-<family>), creates N counter actors and times:
#   cold   - create + first request (fresh actor, restored from the template's golden snapshot)
#   warm   - a second request to the running actor
#   suspend- kubectl ate suspend (snapshot to GCS)
#   resume - first request after suspend (restore from the actor's own snapshot)
# Families whose WorkerPool has no ready worker are skipped.
#
# Usage:
#   substrate-actor-load.sh [--runtime microvm|gvisor] [--actors N] [--families c4,n4,...] [--keep]
# ==============================================================================

set -euo pipefail

ATESPACE="agent-substrate-demo"
RUNTIME="microvm"
ACTORS=3
FAMILIES=""
KEEP=false
PORT=8000

for arg in "$@"; do
  case "${arg}" in
    --runtime=*) RUNTIME="${arg#--runtime=}" ;;
    --actors=*) ACTORS="${arg#--actors=}" ;;
    --families=*) FAMILIES="${arg#--families=}" ;;
    --keep) KEEP=true ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 1 ;;
  esac
done

if [[ -z "${FAMILIES}" ]]; then
  if [[ "${RUNTIME}" == "microvm" ]]; then FAMILIES="c4,n4,c3"; else FAMILIES="c4,c4d,n4,n4d,c3,c3d"; fi
fi

# The atenet router fronts every actor; the target actor is picked by header.
kubectl -n ate-system port-forward svc/atenet-router "${PORT}:80" >/dev/null 2>&1 &
PF=$!
trap 'kill ${PF} 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
  curl -s -o /dev/null "localhost:${PORT}/" && break
  sleep 0.5
done

now() { python3 -c 'import time; print(time.time())'; }
elapsed() { python3 -c "print('%.2f' % ($(now) - $1))"; }

# Returns 0 if the counter actor answered.
hit() {
  curl -sf -m 150 -XPOST -H "ate-target-actor: ${ATESPACE}/$1" "localhost:${PORT}/" >/dev/null
}

ready_workers() {
  kubectl get pods -n "${ATESPACE}" -l "ate.dev/worker-pool=$1" \
    -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
    | grep -c True || true
}

printf "%-14s %-6s %8s %8s %8s %8s\n" TEMPLATE ACTOR COLD_S WARM_S SUSP_S RESUME_S
IFS=',' read -r -a fams <<<"${FAMILIES}"
for fam in "${fams[@]}"; do
  tmpl="${RUNTIME}-${fam}"
  if [[ "$(ready_workers "${tmpl}")" -eq 0 ]]; then
    printf "%-14s skipped (no ready worker in pool %s)\n" "${tmpl}" "${tmpl}"
    continue
  fi
  for i in $(seq 1 "${ACTORS}"); do
    actor="load-${RUNTIME}-${fam}-${i}"
    t0=$(now)
    if ! kubectl ate create actor "${actor}" -a "${ATESPACE}" --template "${tmpl}" >/dev/null 2>&1 \
        || ! hit "${actor}"; then
      state="$(kubectl ate get actors -a "${ATESPACE}" 2>/dev/null | awk -v a="${actor}" '$2 == a {print $4}')"
      printf "%-14s %-6s failed (%s)\n" "${tmpl}" "${i}" "${state:-not created}"
      continue
    fi
    cold=$(elapsed "${t0}")
    t0=$(now); hit "${actor}"; warm=$(elapsed "${t0}")
    t0=$(now); kubectl ate suspend actor "${actor}" -a "${ATESPACE}" >/dev/null; susp=$(elapsed "${t0}")
    t0=$(now); hit "${actor}"; resume=$(elapsed "${t0}")
    printf "%-14s %-6s %8s %8s %8s %8s\n" "${tmpl}" "${i}" "${cold}" "${warm}" "${susp}" "${resume}"
  done
done

if [[ "${KEEP}" == false ]]; then
  for actor in $(kubectl ate get actors -a "${ATESPACE}" 2>/dev/null | awk 'NR>1 && $2 ~ /^load-/ {print $2 ":" $4}'); do
    name="${actor%%:*}"
    [[ "${actor#*:}" == "ACTOR_STATE_RUNNING" ]] && kubectl ate suspend actor "${name}" -a "${ATESPACE}" >/dev/null || true
    kubectl ate delete actor "${name}" -a "${ATESPACE}" >/dev/null || true
  done
fi
