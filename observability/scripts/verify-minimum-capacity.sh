#!/usr/bin/env bash
# ==============================================================================
# verify-minimum-capacity.sh
# Zero-dependency CLI tool to verify ComputeClass proactive minimumCapacity
# (targetNodeCount) fulfillment, per-priority & reservation block accounting,
# proactive scale-up/shortfall diagnostics, and expected scale-down floor protection.
# ==============================================================================

set -euo pipefail

CCC_NAME=""
KUBE_CONTEXT=""
PROJECT_ID=""
CLUSTER_NAME=""
LOCATION=""
MOCK_DIR=""
JSON_OUTPUT=false

usage() {
  cat <<EOF
Usage: $(basename "$0") --ccc <name> [options]

Options:
  --ccc <name>          Name of the ComputeClass to audit (required)
  --context <name>      Kubernetes context to use for live kubectl queries
  --project <id>        GCP Project ID for Cloud Logging visibility queries
  --cluster <name>      GKE Cluster name for Cloud Logging queries
  --location <loc>      Cluster region/zone for Cloud Logging queries
  --mock <dir>          Directory containing mock JSON/YAML files for offline testing
  --json                Emit machine-readable JSON output for alerting pipelines
  -h, --help            Show this help message
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ccc) CCC_NAME="$2"; shift 2 ;;
    --context) KUBE_CONTEXT="$2"; shift 2 ;;
    --project) PROJECT_ID="$2"; shift 2 ;;
    --cluster) CLUSTER_NAME="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --mock) MOCK_DIR="$2"; shift 2 ;;
    --json) JSON_OUTPUT=true; shift 1 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

if [[ -z "$CCC_NAME" ]]; then
  echo "Error: --ccc <name> is required." >&2
  usage
fi

KUBECTL_CMD=(kubectl)
if [[ -n "$KUBE_CONTEXT" ]]; then
  KUBECTL_CMD+=(--context "$KUBE_CONTEXT")
fi

# ------------------------------------------------------------------------------
# 1. Load ComputeClass CRD, Nodes, and CA Visibility Logs
# ------------------------------------------------------------------------------
if [[ -n "$MOCK_DIR" ]]; then
  if [[ ! -f "$MOCK_DIR/computeclass.json" ]]; then
    echo "Error: Missing $MOCK_DIR/computeclass.json" >&2
    exit 1
  fi
  CCC_JSON=$(cat "$MOCK_DIR/computeclass.json")
  NODES_JSON=$(cat "$MOCK_DIR/nodes.json" 2>/dev/null || echo '{"items":[]}')
  LOGS_JSON=$(cat "$MOCK_DIR/ca-logs.json" 2>/dev/null || echo '[]')
else
  CCC_JSON=$("${KUBECTL_CMD[@]}" get computeclass "$CCC_NAME" -o json 2>/dev/null || echo "")
  if [[ -z "$CCC_JSON" ]]; then
    echo "Error: ComputeClass '$CCC_NAME' not found in cluster." >&2
    exit 1
  fi
  NODES_JSON=$("${KUBECTL_CMD[@]}" get nodes -l "cloud.google.com/compute-class=$CCC_NAME" -o json 2>/dev/null || echo '{"items":[]}')

  if [[ -n "$PROJECT_ID" && -n "$CLUSTER_NAME" ]]; then
    FILTER="resource.type=\"k8s_cluster\" AND resource.labels.cluster_name=\"$CLUSTER_NAME\" AND log_id(\"container.googleapis.com/cluster-autoscaler-visibility\") AND (jsonPayload.decision.scaleUp.triggeringPods.controller.name=\"$CCC_NAME\" OR jsonPayload.noDecisionStatus.noScaleUp.unhandledPodGroups.podGroup.samplePod.controller.name=\"$CCC_NAME\" OR jsonPayload.noDecisionStatus.noScaleDown.nodes.nodeName:*)"
    LOGS_JSON=$(gcloud logging read "$FILTER" --project="$PROJECT_ID" --limit=30 --format=json 2>/dev/null | jq '[.[].jsonPayload]' || echo '[]')
  else
    LOGS_JSON='[]'
  fi
fi

# ------------------------------------------------------------------------------
# 2. Extract Declared MinimumCapacity Contract & Precedence Rules
# ------------------------------------------------------------------------------
SPEC_MIN_NODES=$(echo "$CCC_JSON" | jq -r '.spec.minimumCapacity.targetNodeCount // 0')
PRIORITY_SUM_MIN_NODES=$(echo "$CCC_JSON" | jq -r '[.spec.priorities[]?.minimumCapacity.targetNodeCount // 0] | add // 0')

EFFECTIVE_TARGET=$(( SPEC_MIN_NODES > PRIORITY_SUM_MIN_NODES ? SPEC_MIN_NODES : PRIORITY_SUM_MIN_NODES ))
if [[ "$PRIORITY_SUM_MIN_NODES" -gt "$SPEC_MIN_NODES" ]]; then
  PRECEDENCE_MODE="PriorityLevelSum (Sum of priorities[].minimumCapacity [$PRIORITY_SUM_MIN_NODES] > spec.minimumCapacity [$SPEC_MIN_NODES])"
elif [[ "$SPEC_MIN_NODES" -gt 0 ]]; then
  PRECEDENCE_MODE="SpecLevelGlobal (spec.minimumCapacity.targetNodeCount = $SPEC_MIN_NODES)"
else
  PRECEDENCE_MODE="NoneConfigured (targetNodeCount = 0)"
fi

# ------------------------------------------------------------------------------
# 3. Analyze Ready Nodes, Priority Tiers, and Reservation Accounting
# ------------------------------------------------------------------------------
TOTAL_READY_NODES=$(echo "$NODES_JSON" | jq '[.items[]? | select(.status.conditions[]? | select(.type=="Ready" and .status=="True"))] | length')

# Build priority breakdown JSON array
PRIORITY_BREAKDOWN=$(jq -n \
  --argjson ccc "$CCC_JSON" \
  --argjson nodes "$NODES_JSON" \
  '
  ($ccc.spec.priorities // []) | to_entries | map(
    .key as $idx |
    .value as $p |
    ($p.minimumCapacity.targetNodeCount // 0) as $pTarget |
    ($p.machineType // $p.tpu.type // $p.machineFamily // "unspecified") as $shape |
    ($p.spot // false) as $isSpot |
    (($p.reservations.specific[0].name // $p.reservation.specific[0].name) // "none") as $resName |
    # Match nodes by ccc_priority_index annotation OR pre-rollout live inference (machineType + spot)
    ([ $nodes.items[]? | select(.status.conditions[]? | select(.type=="Ready" and .status=="True")) |
       select(
         (.metadata.annotations["ccc_priority_index"] == ($idx | tostring)) or
         ((.metadata.annotations["ccc_priority_index"] == null) and
          ((.metadata.labels["node.kubernetes.io/instance-type"] == $shape) or ($p.tpu != null)) and
          ((.metadata.labels["cloud.google.com/gke-spot"] == "true") == $isSpot))
       )
     ] | length) as $readyCount |
    {
      priorityIndex: ($idx | tostring),
      shape: $shape,
      spot: $isSpot,
      reservation: $resName,
      targetNodeCount: $pTarget,
      readyNodeCount: $readyCount,
      shortfall: (if $pTarget > $readyCount then ($pTarget - $readyCount) else 0 end)
    }
  )
  ')

PRIORITY_SHORTFALL_COUNT=$(echo "$PRIORITY_BREAKDOWN" | jq '[.[].shortfall] | add // 0')

# Determine overall status
if [[ "$EFFECTIVE_TARGET" -eq 0 ]]; then
  OVERALL_STATUS="NOT_CONFIGURED"
elif [[ "$TOTAL_READY_NODES" -ge "$EFFECTIVE_TARGET" && "$PRIORITY_SHORTFALL_COUNT" -eq 0 ]]; then
  OVERALL_STATUS="FULFILLED"
elif [[ "$TOTAL_READY_NODES" -ge "$EFFECTIVE_TARGET" && "$PRIORITY_SHORTFALL_COUNT" -gt 0 ]]; then
  OVERALL_STATUS="FULFILLED_VIA_FALLBACK"
else
  OVERALL_STATUS="SHORTFALL_UNFULFILLED"
fi

# ------------------------------------------------------------------------------
# 4. Inspect Synthetic Placeholder Pods in CA Visibility Logs
# ------------------------------------------------------------------------------
PROACTIVE_SCALEUP_EVENTS=$(echo "$LOGS_JSON" | jq --arg ccc "$CCC_NAME" '
  [ .[]? | select(.decision.scaleUp != null) |
    select(.decision.scaleUp.triggeringPods[]? | (.controller.name == $ccc)) |
    {
      eventId: .decision.eventId,
      decideTime: .decision.decideTime,
      nodepool: (.decision.scaleUp.increasedMigs[0].mig.nodepool // "unknown"),
      zone: (.decision.scaleUp.increasedMigs[0].mig.zone // "unknown"),
      requestedNodes: (.decision.scaleUp.increasedMigs[0].requestedNodes // 0),
      syntheticPods: [ .decision.scaleUp.triggeringPods[]? | .name ]
    }
  ]')

SHORTFALL_NO_SCALEUP_EVENTS=$(echo "$LOGS_JSON" | jq --arg ccc "$CCC_NAME" '
  [ .[]? | select(.noDecisionStatus.noScaleUp != null) |
    .noDecisionStatus.noScaleUp.unhandledPodGroups[]? |
    select(.podGroup.samplePod.controller.name == $ccc) |
    {
      samplePod: .podGroup.samplePod.name,
      unhandledPodCount: .podGroup.totalPodCount,
      napFailureReason: (.napFailureReasons[0].messageId // "none"),
      rejectedMigsCount: (.rejectedMigs | length),
      primaryRejectionReason: (.rejectedMigs[0].reason.messageId // "no.candidate.migs")
    }
  ]')

FLOOR_PROTECTION_NOSCALEDOWN=$(echo "$LOGS_JSON" | jq '
  [ .[]? | select(.noDecisionStatus.noScaleDown != null) |
    .noDecisionStatus.noScaleDown.nodes[]? |
    select(.reason.messageId == "no.scale.down.node.no.place.to.move.pods") |
    .nodeName
  ] | unique')

ORPHANED_DELETED_NODES=$(echo "$NODES_JSON" | jq '[.items[]? | select(.metadata.annotations["ccc_priority_index"] == "ccc_deleted") | .metadata.name]')

# ------------------------------------------------------------------------------
# 5. Output Results (JSON or Human-Readable CLI Report)
# ------------------------------------------------------------------------------
if [[ "$JSON_OUTPUT" == "true" ]]; then
  jq -n \
    --arg ccc "$CCC_NAME" \
    --arg status "$OVERALL_STATUS" \
    --arg mode "$PRECEDENCE_MODE" \
    --argjson specTarget "$SPEC_MIN_NODES" \
    --argjson prioritySumTarget "$PRIORITY_SUM_MIN_NODES" \
    --argjson effectiveTarget "$EFFECTIVE_TARGET" \
    --argjson totalReady "$TOTAL_READY_NODES" \
    --argjson priorities "$PRIORITY_BREAKDOWN" \
    --argjson scaleUps "$PROACTIVE_SCALEUP_EVENTS" \
    --argjson shortfalls "$SHORTFALL_NO_SCALEUP_EVENTS" \
    --argjson floorProtectedNodes "$FLOOR_PROTECTION_NOSCALEDOWN" \
    --argjson orphanedNodes "$ORPHANED_DELETED_NODES" \
    '{
      computeClass: $ccc,
      status: $status,
      precedenceMode: $mode,
      specTargetNodeCount: $specTarget,
      prioritySumTargetNodeCount: $prioritySumTarget,
      effectiveTargetNodeCount: $effectiveTarget,
      totalReadyNodes: $totalReady,
      globalShortfall: (if $effectiveTarget > $totalReady then ($effectiveTarget - $totalReady) else 0 end),
      priorityBreakdown: $priorities,
      proactiveScaleUpEvents: $scaleUps,
      shortfallDiagnostics: $shortfalls,
      floorProtectedNodesWAI: $floorProtectedNodes,
      orphanedDeletedNodes: $orphanedNodes
    }'
  exit 0
fi

echo "================================================================================"
echo " GKE ComputeClass Proactive MinimumCapacity Verification & Shortfall Audit"
echo "================================================================================"
echo "ComputeClass:          $CCC_NAME"
echo "Precedence Policy:     $PRECEDENCE_MODE"
echo "Effective Node Floor:  $EFFECTIVE_TARGET node(s) required"
echo "Current Ready Nodes:   $TOTAL_READY_NODES node(s) active"
echo "Fulfillment Status:    $OVERALL_STATUS"
echo ""
echo "[1] Per-Priority & Reservation Block Accounting"
echo "--------------------------------------------------------------------------------"
echo "$PRIORITY_BREAKDOWN" | jq -r '.[] | "  • Priority \(.priorityIndex) (\(.shape), Spot=\(.spot), Reservation=\(.reservation)):\n      Target Floor: \(.targetNodeCount) | Ready Nodes: \(.readyNodeCount) | Tier Shortfall: \(.shortfall)"'
echo ""
echo "[2] Proactive Scale-Up Telemetry (Cluster Autoscaler Visibility)"
echo "--------------------------------------------------------------------------------"
SCALEUP_COUNT=$(echo "$PROACTIVE_SCALEUP_EVENTS" | jq 'length')
if [[ "$SCALEUP_COUNT" -gt 0 ]]; then
  echo "$PROACTIVE_SCALEUP_EVENTS" | jq -r '.[] | "  • Event ID: \(.eventId)\n      Target NodePool: \(.nodepool) (Zone: \(.zone)) -> Requested Nodes: +\(.requestedNodes)\n      Triggering Target: \(.syntheticPods | join(", "))"'
else
  echo "  No recent decision.scaleUp events found for proactive minimumCapacity targets."
fi
echo ""
echo "[3] Shortfall & Failure Diagnostics (noDecisionStatus.noScaleUp)"
echo "--------------------------------------------------------------------------------"
SHORTFALL_EVENTS_COUNT=$(echo "$SHORTFALL_NO_SCALEUP_EVENTS" | jq 'length')
if [[ "$SHORTFALL_EVENTS_COUNT" -gt 0 ]]; then
  echo "$SHORTFALL_NO_SCALEUP_EVENTS" | jq -r '.[] | "  • Unhandled Capacity Floor Target: \(.samplePod) (\(.unhandledPodCount) node(s) stalled)\n      NAP Failure Reason:       \(.napFailureReason)\n      Primary MIG Rejection:    \(.primaryRejectionReason) (\(.rejectedMigsCount) candidate MIGs rejected)\n      Remediation Guidance:     Verify GCE reservation block capacity (usedCount vs totalCount), check for degraded hosts in TPU/GPU slices, or add a fallback priority tier."'
else
  echo "  No unhandled proactive capacity floor targets detected in noScaleUp logs."
fi
echo ""
echo "[4] Scale-Down Floor Protection & Lifecycle Checks"
echo "--------------------------------------------------------------------------------"
FLOOR_COUNT=$(echo "$FLOOR_PROTECTION_NOSCALEDOWN" | jq 'length')
if [[ "$FLOOR_COUNT" -gt 0 ]]; then
  echo "  • [EXPECTED WAI] Nodes protected from scale-down by minimumCapacity floor:"
  echo "$FLOOR_PROTECTION_NOSCALEDOWN" | jq -r '.[] | "      - \(.) (Log reason: no.scale.down.node.no.place.to.move.pods -> WAI floor enforcement)"'
else
  echo "  • Floor protection logs: No active scale-down rejections logged in window."
fi

ORPHAN_COUNT=$(echo "$ORPHANED_DELETED_NODES" | jq 'length')
if [[ "$ORPHAN_COUNT" -gt 0 ]]; then
  echo "  • [WARNING] Detected $ORPHAN_COUNT orphaned node(s) with ccc_priority_index=ccc_deleted:"
  echo "$ORPHANED_DELETED_NODES" | jq -r '.[] | "      - \(.) (Parent CCC deleted; node pool untracked until CA scale-down timer expires)"'
fi
echo "================================================================================"
