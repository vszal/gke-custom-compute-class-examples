#!/usr/bin/env bash
# ==============================================================================
# monitor-hard-stockouts.sh
# Hard stockout detection and alerting tool for GKE ComputeClasses.
# Triangulates:
#   1. ComputeClassStatus (all priorities in ProvisioningSuspended / RuleMisconfigured)
#      OR Pre-Rollout Live Inference (spec.priorities + Pending Pods + 0 scaled nodes)
#   2. Cluster Autoscaler Visibility Logs (noDecisionStatus.noScaleUp with unhandledPodGroups)
#   3. Kubernetes Pod Warning Events (Warning FailedScaleUp / NotTriggerScaleUp)
#
# Usage:
#   monitor-hard-stockouts.sh --ccc <name> [--context <ctx>] [--project <id>] [--cluster <name>]
#   monitor-hard-stockouts.sh --mock <dir> [--ccc <name>]
# ==============================================================================

set -euo pipefail

CCC_NAME=""
KUBE_CONTEXT=""
PROJECT_ID=""
CLUSTER_NAME=""
LOCATION=""
MOCK_DIR=""
OUTPUT_JSON=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --ccc <name>         Name of ComputeClass to evaluate
  --context <ctx>      Kubernetes kubeconfig context to use (optional)
  --mock <dir>         Path to mock directory containing computeclass.json, visibility-logs.json, pod-events.json
  --project <id>       Google Cloud Project ID (required for live CA visibility log query)
  --cluster <name>     GKE Cluster Name (required for live CA visibility log query)
  --location <loc>     GKE Cluster Location (region or zone)
  --json               Output structured JSON report
  -h, --help           Show this help message
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ccc) CCC_NAME="$2"; shift 2 ;;
    --context) KUBE_CONTEXT="$2"; shift 2 ;;
    --mock) MOCK_DIR="$2"; shift 2 ;;
    --project) PROJECT_ID="$2"; shift 2 ;;
    --cluster) CLUSTER_NAME="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --json) OUTPUT_JSON=true; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

if ! command -v jq &>/dev/null; then
  echo "Error: 'jq' is required but not installed." >&2
  exit 1
fi

KUBECTL_ARGS=()
if [[ -n "$KUBE_CONTEXT" ]]; then
  KUBECTL_ARGS+=("--context=$KUBE_CONTEXT")
fi

# Load ComputeClass data
if [[ -n "$MOCK_DIR" ]]; then
  if [[ ! -f "$MOCK_DIR/computeclass.json" ]]; then
    echo "Error: Mock file $MOCK_DIR/computeclass.json not found." >&2
    exit 1
  fi
  CCC_DATA=$(cat "$MOCK_DIR/computeclass.json")
  if [[ -z "$CCC_NAME" ]]; then
    CCC_NAME=$(echo "$CCC_DATA" | jq -r '.metadata.name // "unknown-ccc"')
  fi
else
  if [[ -z "$CCC_NAME" ]]; then
    echo "Error: --ccc is required in live mode." >&2
    exit 1
  fi
  if ! command -v kubectl &>/dev/null; then
    echo "Error: 'kubectl' is required in live mode." >&2
    exit 1
  fi
  CCC_DATA=$(kubectl "${KUBECTL_ARGS[@]}" get computeclass "$CCC_NAME" -o json)
fi

WHEN_UNSATISFIABLE=$(echo "$CCC_DATA" | jq -r '.spec.whenUnsatisfiable // "DoNotScaleUp"')
PRIORITY_STATUSES=$(echo "$CCC_DATA" | jq -c '.status.priorityStatuses // []')
SPEC_PRIORITIES=$(echo "$CCC_DATA" | jq -c '.spec.priorities // []')
STATUS_MODE="CRD status.priorityStatuses"

# Load CA Visibility Logs
VIS_LOGS="[]"
if [[ -n "$MOCK_DIR" ]]; then
  if [[ -f "$MOCK_DIR/visibility-logs.json" ]]; then
    VIS_LOGS=$(cat "$MOCK_DIR/visibility-logs.json")
  fi
else
  if [[ -n "$PROJECT_ID" && -n "$CLUSTER_NAME" ]]; then
    FILTER="log_id(\"container.googleapis.com/cluster-autoscaler-visibility\") AND resource.labels.cluster_name=\"$CLUSTER_NAME\" AND jsonPayload.noDecisionStatus.noScaleUp:*"
    VIS_LOGS=$(gcloud logging read "$FILTER" --project="$PROJECT_ID" --format=json --limit=10 2>/dev/null || echo "[]")
  fi
fi

# In live mode, identify pods belonging to this ComputeClass to scope events/logs
CCC_POD_NAMES_JSON="[]"
if [[ -z "$MOCK_DIR" ]] && command -v kubectl &>/dev/null; then
  CCC_POD_NAMES_JSON=$(kubectl "${KUBECTL_ARGS[@]}" get pods --all-namespaces -o json 2>/dev/null | jq -c --arg ccc "$CCC_NAME" '
    [ .items[]? | select((.spec.nodeSelector["cloud.google.com/compute-class"] // "") == $ccc) | .metadata.name ]
  ' || echo "[]")
fi

# Extract unhandled pod groups from noScaleUp
UNHANDLED_POD_GROUPS=$(echo "$VIS_LOGS" | jq -c --argjson cccPods "$CCC_POD_NAMES_JSON" --arg isMock "$MOCK_DIR" '
  if type == "array" then . else [.] end |
  map(.jsonPayload.noDecisionStatus.noScaleUp.unhandledPodGroups // []) |
  flatten |
  unique_by(.podGroup.samplePod.name) |
  if ($isMock != "" or ($cccPods | length) == 0) then .
  else map(select(.podGroup.samplePod.name as $pname | ($cccPods | index($pname)) != null))
  end
')

UNHANDLED_POD_COUNT=$(echo "$UNHANDLED_POD_GROUPS" | jq '
  [ .[]?.podGroup.totalPodCount // 0 ] | add // 0
')

NO_SCALEUP_REASON=$(echo "$VIS_LOGS" | jq -r '
  if type == "array" then . else [.] end |
  map(.jsonPayload.noDecisionStatus.noScaleUp.reason.messageId // (.jsonPayload.noDecisionStatus.noScaleUp.unhandledPodGroups[0]?.rejectedMigs[0]?.reason.messageId) // empty) |
  map(select(. != "")) |
  first // "N/A"
')

# Load Pod Events
POD_EVENTS="[]"
if [[ -n "$MOCK_DIR" ]]; then
  if [[ -f "$MOCK_DIR/pod-events.json" ]]; then
    POD_EVENTS=$(cat "$MOCK_DIR/pod-events.json" | jq -c '.items // []')
  fi
else
  if command -v kubectl &>/dev/null; then
    POD_EVENTS=$(kubectl "${KUBECTL_ARGS[@]}" get events --all-namespaces -o json 2>/dev/null | jq -c --argjson cccPods "$CCC_POD_NAMES_JSON" '
      [ .items[]? |
        select(.reason == "FailedScaleUp" or .reason == "NotTriggerScaleUp") |
        select(($cccPods | length) == 0 or (.involvedObject.name as $n | ($cccPods | index($n)) != null))
      ]
    ' || echo "[]")
  fi
fi

FAILED_SCALEUP_EVENTS=$(echo "$POD_EVENTS" | jq -c '
  [ .[]? |
    select(.reason == "FailedScaleUp" or .reason == "NotTriggerScaleUp") |
    {
      pod: .involvedObject.name,
      namespace: .involvedObject.namespace,
      reason: .reason,
      message: .message,
      count: (.count // 1),
      lastTimestamp: (.lastTimestamp // .eventTime // "recent")
    }
  ]
')

FAILED_SCALEUP_COUNT=$(echo "$FAILED_SCALEUP_EVENTS" | jq 'length')

# Evaluate priorities (CRD status vs Pre-Rollout Live Inference)
if [[ $(echo "$PRIORITY_STATUSES" | jq 'length') -gt 0 ]]; then
  TOTAL_PRIORITIES=$(echo "$PRIORITY_STATUSES" | jq 'length')
  SUSPENDED_RULES=$(echo "$PRIORITY_STATUSES" | jq -c '
    [ .[] |
      select(any(.conditions[]?; (.type == "ProvisioningSuspended" or .type == "ProvisioningConstrained") and .status == "True")) |
      {
        identifier: .identifier,
        conditionType: ([.conditions[]? | select((.type == "ProvisioningSuspended" or .type == "ProvisioningConstrained") and .status == "True") | .type] | first // "ProvisioningSuspended"),
        reason: ([.conditions[]? | select((.type == "ProvisioningSuspended" or .type == "ProvisioningConstrained") and .status == "True") | .reason] | first // "Unknown"),
        message: ([.conditions[]? | select((.type == "ProvisioningSuspended" or .type == "ProvisioningConstrained") and .status == "True") | .message] | first // ""),
        until: ([.conditions[]? | select((.type == "ProvisioningSuspended" or .type == "ProvisioningConstrained") and .status == "True") | .message | capture("until (?<until>[^.]+)").until] | first // null)
      }
    ]
  ')

  MISCONFIGURED_RULES=$(echo "$PRIORITY_STATUSES" | jq -c '
    [ .[] |
      select(any(.conditions[]?; .type == "RuleMisconfigured" and .status == "True")) |
      {
        identifier: .identifier,
        reason: ([.conditions[]? | select(.type == "RuleMisconfigured") | .reason] | first // "Unknown"),
        message: ([.conditions[]? | select(.type == "RuleMisconfigured") | .message] | first // "")
      }
    ]
  ')

  ACTIVE_RULES=$(echo "$PRIORITY_STATUSES" | jq -c '
    [ .[] |
      select(
        (any(.conditions[]?; (.type == "ProvisioningSuspended" or .type == "ProvisioningConstrained") and .status == "True") | not) and
        (any(.conditions[]?; .type == "RuleMisconfigured" and .status == "True") | not)
      )
    ]
  ')

  SUSPENDED_COUNT=$(echo "$SUSPENDED_RULES" | jq 'length')
  MISCONFIGURED_COUNT=$(echo "$MISCONFIGURED_RULES" | jq 'length')
  ACTIVE_COUNT=$(echo "$ACTIVE_RULES" | jq 'length')

  EARLIEST_UNTIL=$(echo "$SUSPENDED_RULES" | jq -r '
    [ .[] | select(.until != null) | .until ] | sort | first // "N/A"
  ')
else
  # Pre-Rollout Live Inference Mode
  STATUS_MODE="Pre-Rollout Live Inference (spec.priorities + CA Visibility + Pod Warning Events)"
  TOTAL_PRIORITIES=$(echo "$SPEC_PRIORITIES" | jq 'length')
  # Check if there are pending pods targeting this ComputeClass
  PENDING_CCC_PODS=0
  SCALED_CCC_NODES=0
  if [[ -z "$MOCK_DIR" ]] && command -v kubectl &>/dev/null; then
    PENDING_CCC_PODS=$(kubectl "${KUBECTL_ARGS[@]}" get pods --all-namespaces -o json 2>/dev/null | jq --arg ccc "$CCC_NAME" '
      [ .items[]? | select(
          .status.phase == "Pending" and
          ((.spec.nodeSelector["cloud.google.com/compute-class"] // "") == $ccc)
        )
      ] | length
    ' || echo 0)
    SCALED_CCC_NODES=$(kubectl "${KUBECTL_ARGS[@]}" get nodes -l "cloud.google.com/compute-class=$CCC_NAME" -o json 2>/dev/null | jq '.items | length' || echo 0)
  fi

  if [[ "$UNHANDLED_POD_COUNT" -eq 0 && "$PENDING_CCC_PODS" -gt 0 ]]; then
    UNHANDLED_POD_COUNT=$PENDING_CCC_PODS
  fi

  if [[ "$PENDING_CCC_PODS" -gt 0 && "$SCALED_CCC_NODES" -eq 0 && ( "$FAILED_SCALEUP_COUNT" -gt 0 || "$UNHANDLED_POD_COUNT" -gt 0 ) ]]; then
    SUSPENDED_COUNT=$TOTAL_PRIORITIES
    MISCONFIGURED_COUNT=0
    ACTIVE_COUNT=0
    EARLIEST_UNTIL="Active CA Cooldown (Live Event Inference)"
    SAMPLE_EVENT_MSG=$(echo "$FAILED_SCALEUP_EVENTS" | jq -r '.[0].message // "All declared ComputeClass priorities failed scale-up evaluation"')
    SUSPENDED_RULES=$(echo "$SPEC_PRIORITIES" | jq -c --arg msg "$SAMPLE_EVENT_MSG" '
      to_entries | map({
        identifier: (.key | tostring),
        conditionType: "ProvisioningSuspended",
        reason: (if (.value.reservations // .value.reservationAffinity) then "ReservationCapacityExhausted" else "UnsatisfiablePriorityRule" end),
        message: ("Spec rule " + (.value | tostring) + " failed scale-up. Latest CA Event: " + $msg),
        until: "Active CA Cooldown"
      })
    ')
    MISCONFIGURED_RULES="[]"
  else
    SUSPENDED_COUNT=0
    MISCONFIGURED_COUNT=0
    ACTIVE_COUNT=$TOTAL_PRIORITIES
    EARLIEST_UNTIL="N/A"
    SUSPENDED_RULES="[]"
    MISCONFIGURED_RULES="[]"
  fi
fi

# Check hard stockout condition:
IS_HARD_STOCKOUT=false
if [[ "$TOTAL_PRIORITIES" -gt 0 && "$ACTIVE_COUNT" -eq 0 ]]; then
  IS_HARD_STOCKOUT=true
elif [[ "$WHEN_UNSATISFIABLE" == "DoNotScaleUp" && "$UNHANDLED_POD_COUNT" -gt 0 && "$FAILED_SCALEUP_COUNT" -gt 0 ]]; then
  IS_HARD_STOCKOUT=true
fi

if [[ "$OUTPUT_JSON" == true ]]; then
  jq -n \
    --arg ccc "$CCC_NAME" \
    --arg statusMode "$STATUS_MODE" \
    --arg whenUnsat "$WHEN_UNSATISFIABLE" \
    --argjson totalPriorities "$TOTAL_PRIORITIES" \
    --argjson suspendedCount "$SUSPENDED_COUNT" \
    --argjson misconfiguredCount "$MISCONFIGURED_COUNT" \
    --argjson activeCount "$ACTIVE_COUNT" \
    --arg earliestUntil "$EARLIEST_UNTIL" \
    --arg isHardStockout "$IS_HARD_STOCKOUT" \
    --arg noScaleupReason "$NO_SCALEUP_REASON" \
    --argjson unhandledPods "$UNHANDLED_POD_COUNT" \
    --argjson unhandledPodGroups "$UNHANDLED_POD_GROUPS" \
    --argjson suspendedRules "$SUSPENDED_RULES" \
    --argjson failedScaleupEvents "$FAILED_SCALEUP_EVENTS" \
    '{
      computeClass: $ccc,
      statusEvaluationMode: $statusMode,
      whenUnsatisfiable: $whenUnsat,
      evaluation: {
        isHardStockout: ($isHardStockout == "true"),
        severity: (if $isHardStockout == "true" then "CRITICAL" else "OK" end),
        totalPriorities: $totalPriorities,
        activePriorities: $activeCount,
        suspendedPriorities: $suspendedCount,
        misconfiguredPriorities: $misconfiguredCount,
        earliestBackoffExpiration: $earliestUntil
      },
      workloadImpact: {
        totalUnhandledPods: $unhandledPods,
        caVisibilityReason: $noScaleupReason,
        unhandledPodGroups: $unhandledPodGroups,
        failedScaleUpEventCount: ($failedScaleupEvents | length),
        failedScaleUpEvents: $failedScaleupEvents
      },
      priorityDetails: {
        suspendedRules: $suspendedRules
      }
    }'
  exit 0
fi

# Human-readable CLI report
cat <<EOF
================================================================================
GKE ComputeClass Hard Stockout Monitoring & Alert Report
================================================================================

[1] Target ComputeClass Overview:
    ComputeClass:               $CCC_NAME
    Evaluation Mode:            $STATUS_MODE
    whenUnsatisfiable:          $WHEN_UNSATISFIABLE
    Total Priority Rules:       $TOTAL_PRIORITIES
    Active Provisioning Rules:  $ACTIVE_COUNT
    Suspended Rules (Cooldown): $SUSPENDED_COUNT
    Misconfigured Rules:        $MISCONFIGURED_COUNT

[2] Stockout Status & Severity Assessment:
EOF

if [[ "$IS_HARD_STOCKOUT" == true ]]; then
  cat <<EOF
    STATUS:                     CRITICAL - HARD STOCKOUT ACTIVE
    Impact:                     All declared priorities ($TOTAL_PRIORITIES/$TOTAL_PRIORITIES) are unable to scale up.
                                Workloads targeting '$CCC_NAME' are unschedulable ($WHEN_UNSATISFIABLE).
    Earliest Backoff Expiry:    $EARLIEST_UNTIL
EOF
else
  cat <<EOF
    STATUS:                     HEALTHY / PARTIAL
    Impact:                     $ACTIVE_COUNT priority rules are active and available for scale-up.
EOF
fi

cat <<EOF

[3] Priority Status Breakdown:
EOF

if [[ "$SUSPENDED_COUNT" -gt 0 ]]; then
  echo "    Suspended / Unsatisfiable Priority Rules ($SUSPENDED_COUNT):"
  for i in $(seq 0 $((SUSPENDED_COUNT - 1))); do
    RULE=$(echo "$SUSPENDED_RULES" | jq -c ".[$i]")
    R_ID=$(echo "$RULE" | jq -r '.identifier')
    R_REASON=$(echo "$RULE" | jq -r '.reason')
    R_UNTIL=$(echo "$RULE" | jq -r '.until // "N/A"')
    R_MSG=$(echo "$RULE" | jq -r '.message')
    echo "      • Priority [identifier: \"$R_ID\"]"
    echo "        Reason:        $R_REASON"
    echo "        Backoff Until: $R_UNTIL"
    echo "        Detail:        $R_MSG"
  done
fi

if [[ "$MISCONFIGURED_COUNT" -gt 0 ]]; then
  echo "    Misconfigured Priority Rules ($MISCONFIGURED_COUNT):"
  for i in $(seq 0 $((MISCONFIGURED_COUNT - 1))); do
    RULE=$(echo "$MISCONFIGURED_RULES" | jq -c ".[$i]")
    R_ID=$(echo "$RULE" | jq -r '.identifier')
    R_REASON=$(echo "$RULE" | jq -r '.reason')
    R_MSG=$(echo "$RULE" | jq -r '.message')
    echo "      • Priority [identifier: \"$R_ID\"]"
    echo "        Reason: $R_REASON"
    echo "        Detail: $R_MSG"
  done
fi

cat <<EOF

[4] Cluster Autoscaler Visibility & Workload Impact:
    No Scale-Up Reason:         $NO_SCALEUP_REASON
    Total Unsatisfied Pods:     $UNHANDLED_POD_COUNT
    Unhandled Pod Groups:       $(echo "$UNHANDLED_POD_GROUPS" | jq 'length')
EOF

if [[ "$UNHANDLED_POD_COUNT" -gt 0 && $(echo "$UNHANDLED_POD_GROUPS" | jq 'length') -gt 0 ]]; then
  echo "    Pod Groups Waiting on Capacity:"
  echo "$UNHANDLED_POD_GROUPS" | jq -r '
    .[]? | "      • " + (.podGroup.samplePod.namespace // "default") + "/" + (.podGroup.samplePod.name // "unknown") + " (" + ((.podGroup.totalPodCount // 1) | tostring) + " pods)"
  '
fi

cat <<EOF

[5] Kubernetes Pod Warning Events (FailedScaleUp / NotTriggerScaleUp):
    Total Warning Events:       $FAILED_SCALEUP_COUNT
EOF

if [[ "$FAILED_SCALEUP_COUNT" -gt 0 ]]; then
  echo "$FAILED_SCALEUP_EVENTS" | jq -r '
    .[]? | "      • Pod: " + .namespace + "/" + .pod + " [" + .reason + "] (x" + (.count | tostring) + ")\n        Message: " + .message
  '
fi

if [[ "$IS_HARD_STOCKOUT" == true ]]; then
  cat <<EOF

[6] Recommended Operational Actions (Runbook):
    1. Expand Priorities: Add alternative machine families or Spot/On-Demand fallback tiers.
    2. Review Reservation/Zonal Pinning: Verify specific reservations have available capacity in target zones.
    3. Modify Unsatisfiable Policy: Consider setting whenUnsatisfiable: ScaleUpAnyway
       if fallback to standard cluster node pools is acceptable during stockouts.
    4. Backoff Expiration: Next automated evaluation occurs at $EARLIEST_UNTIL.
================================================================================
EOF
else
  echo "================================================================================"
fi
