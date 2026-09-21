#!/usr/bin/env bash
#
# install-dashboards.sh — install the ComputeClass priority-fulfillment dashboards
# into any Google Cloud project.
#
#   ./install-dashboards.sh                 # uses the active gcloud project
#   ./install-dashboards.sh my-project      # explicit project
#   ./install-dashboards.sh --update        # replace dashboards already installed
#   ./install-dashboards.sh --validate-only # parse + validate, create nothing
#   ./install-dashboards.sh --all           # include accelerator variants with no data yet
#
# The dashboards read the metrics emitted by exporter.yaml through Google Cloud
# Managed Service for Prometheus. They contain no project, cluster, or
# ComputeClass names — every filter defaults to `.*` — so the same JSON installs
# unchanged in any project. The GPU and TPU fleet variants are skipped when the
# project reports no chips of that kind; --all installs them regardless.
#
# Requirements: gcloud, curl. No jq, no Terraform, no local state.

set -euo pipefail

cd "$(dirname "$0")"

DASHBOARDS=(
  dashboard.json            # one ComputeClass, in detail
  dashboard-health.json     # is scale-up working right now
  dashboard-fleet.json      # every class and cluster, by vCPU
  dashboard-fleet-gpu.json  # ... by GPU chip
  dashboard-fleet-tpu.json  # ... by TPU chip
)

PROJECT=""
UPDATE=false
VALIDATE_ONLY=false
ALL=false

for arg in "$@"; do
  case "$arg" in
    --update)        UPDATE=true ;;
    --validate-only) VALIDATE_ONLY=true ;;
    --all)           ALL=true ;;
    -h|--help)       sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)              echo "unknown flag: $arg" >&2; exit 2 ;;
    *)               PROJECT="$arg" ;;
  esac
done

if [[ -z "$PROJECT" ]]; then
  PROJECT="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
fi
if [[ -z "$PROJECT" || "$PROJECT" == "(unset)" ]]; then
  echo "No project. Pass one as an argument, set PROJECT_ID, or run 'gcloud config set project'." >&2
  exit 2
fi

echo "Project: $PROJECT"

# --- preflight: are the exporter's metrics actually arriving? -----------------
# Warn only. An empty dashboard is a confusing first impression, so say up front
# whether the data is there.
TOKEN=""
promq() {
  curl -s -H "Authorization: Bearer $TOKEN" \
    "https://monitoring.googleapis.com/v1/projects/$PROJECT/location/global/prometheus/api/v1/query" \
    --data-urlencode "query=$1" | tr -d ' \n'
}

preflight() {
  TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
  [[ -z "$TOKEN" ]] && { echo "  ! could not get an access token; skipping metric preflight"; return; }

  local series
  series="$(promq 'ccc_exporter_up')"
  if [[ "$series" == *'"result":[]'* ]]; then
    echo "  ! no ccc_exporter_up series in this project (last 5m)."
    echo "    Deploy the exporter first:  kubectl apply -f exporter.yaml"
    echo "    It needs Managed Service for Prometheus enabled on the cluster:"
    echo "    gcloud container clusters update CLUSTER --location LOCATION --enable-managed-prometheus"
  elif [[ "$series" == *'"status":"success"'* ]]; then
    echo "  ✓ exporter metrics are arriving"
  else
    echo "  ! exporter preflight inconclusive; continuing"
  fi

  # The health dashboard's unschedulable-pod tiles come from GKE's managed
  # kube-state-metrics, not from the exporter. Both queries end in `or vector(0)`,
  # so without the POD package they read a steady, believable zero forever —
  # worth catching here rather than trusting later.
  series="$(promq 'count(kube_pod_status_phase)')"
  if [[ "$series" == *'"result":[]'* ]]; then
    echo "  ! no kube-state-metrics in this project: the health dashboard's"
    echo "    unschedulable-pod tiles will read 0 whether or not pods are stuck."
    echo "    gcloud container clusters update CLUSTER --location LOCATION --monitoring=SYSTEM,POD"
    echo "    (--monitoring replaces the component set; list everything you want to keep)"
  elif [[ "$series" == *'"status":"success"'* ]]; then
    echo "  ✓ kube-state-metrics present (unschedulable-pod tiles will work)"
  fi
}
echo "Preflight:"
preflight

# --- install ------------------------------------------------------------------
display_name() {
  # Pull displayName out of the JSON without requiring jq.
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["displayName"])' "$1" 2>/dev/null \
    || grep -m1 '"displayName"' "$1" | sed 's/.*"displayName" *: *"\(.*\)".*/\1/'
}

# A dashboard whose every tile is blank is indistinguishable from a broken
# exporter, so the accelerator variants are only installed when the fleet
# actually reports that kind of chip. --all overrides, for a fleet that will
# have TPUs next week.
guard_query() {
  case "$1" in
    dashboard-fleet-gpu.json) echo 'ccc_accelerators_by_priority{accelerator="nvidia.com/gpu"}' ;;
    dashboard-fleet-tpu.json) echo 'ccc_accelerators_by_priority{accelerator="google.com/tpu"}' ;;
    *) echo "" ;;
  esac
}

existing_id() {
  gcloud monitoring dashboards list --project="$PROJECT" \
    --filter="displayName=\"$1\"" --format="value(name)" 2>/dev/null | head -n1
}

for f in "${DASHBOARDS[@]}"; do
  name="$(display_name "$f")"
  echo
  echo "== $f — \"$name\""

  guard="$(guard_query "$f")"
  if [[ -n "$guard" && -n "$TOKEN" ]] && ! $ALL && ! $VALIDATE_ONLY; then
    if [[ "$(promq "$guard")" == *'"result":[]'* ]]; then
      echo "  · skipped: no such accelerator reporting in this project (pass --all to install anyway)"
      continue
    fi
  fi

  if $VALIDATE_ONLY; then
    gcloud monitoring dashboards create --project="$PROJECT" \
      --config-from-file="$f" --validate-only && echo "  ✓ valid"
    continue
  fi

  id="$(existing_id "$name")"
  if [[ -n "$id" ]]; then
    if $UPDATE; then
      # Cloud Monitoring rejects an update whose payload has no etag, and the
      # files here deliberately carry none (an etag is per-installation state,
      # not portable content). So read the live etag and splice it in.
      etag="$(gcloud monitoring dashboards describe "$id" --project="$PROJECT" \
                --format="value(etag)" 2>/dev/null)"
      if [[ -z "$etag" ]]; then
        echo "  ! could not read the current etag; skipping (delete it and re-run to recreate)"
        continue
      fi
      tmp="$(mktemp -t ccc-dashboard)"
      python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
d["name"], d["etag"] = sys.argv[2], sys.argv[3]
json.dump(d, open(sys.argv[4], "w"))' "$f" "$id" "$etag" "$tmp"
      gcloud monitoring dashboards update "$id" --project="$PROJECT" \
        --config-from-file="$tmp" >/dev/null
      rm -f "$tmp"
      echo "  ✓ updated"
    else
      echo "  · already installed (pass --update to replace it)"
    fi
  else
    id="$(gcloud monitoring dashboards create --project="$PROJECT" \
      --config-from-file="$f" --format="value(name)")"
    echo "  ✓ created"
  fi

  # Cloud Monitoring has no dashboard-level default time range — it lives in the
  # URL. PT24H matches the "last 24h" scorecards on these dashboards.
  echo "  https://console.cloud.google.com/monitoring/dashboards/builder/${id##*/};duration=PT24H?project=$PROJECT"
done

echo
echo "Done. Bookmark the URLs above — the ;duration=PT24H is what sets the time range."
