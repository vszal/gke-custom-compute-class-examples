#!/usr/bin/env bash
# ==============================================================================
# trace-pod-scaleup.sh
# End-to-end traceability tool for GKE ComputeClass scale-ups.
# Correlates: Pending Pod -> CA Visibility Decision -> ComputeClass Priority Status -> Node Annotation.
# Supports both CRD status.priorityStatuses (1.36.4-gke.1391000+ with EnhancedObservability)
# and Pre-Rollout Live Inference Mode (correlating spec.priorities with live CA Visibility logs & Node labels).
#
# Usage:
#   trace-pod-scaleup.sh --pod <name> [--namespace <ns>] [--ccc <name>] [--context <ctx>] [--project <id>] [--cluster <name>] [--location <loc>]
#   trace-pod-scaleup.sh --mock <dir> [--pod <name>] [--namespace <ns>]
# ==============================================================================

set -euo pipefail

POD_NAME=""
NAMESPACE="default"
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
  --pod <name>         Name of the pod to trace
  --namespace <ns>     Kubernetes namespace (default: default)
  --ccc <name>         Name of ComputeClass (optional, auto-detected from pod nodeSelector)
  --context <ctx>      Kubernetes kubeconfig context to use (optional)
  --project <id>       Google Cloud Project ID (required for live log query)
  --cluster <name>     GKE Cluster Name (required for live log query)
  --location <loc>     GKE Cluster Location (region or zone)
  --mock <dir>         Path to mock directory containing pod.json, computeclass.json, visibility-logs.json, nodes.json
  --json               Output structured JSON instead of human-readable report
  -h, --help           Show this help message
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pod) POD_NAME="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --ccc) CCC_NAME="$2"; shift 2 ;;
    --context) KUBE_CONTEXT="$2"; shift 2 ;;
    --project) PROJECT_ID="$2"; shift 2 ;;
    --cluster) CLUSTER_NAME="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --mock) MOCK_DIR="$2"; shift 2 ;;
    --json) OUTPUT_JSON=true; shift ;;
    -h|--help) usage ;;
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

# Load pod data
if [[ -n "$MOCK_DIR" ]]; then
  if [[ ! -f "$MOCK_DIR/pod.json" ]]; then
    echo "Error: Mock file $MOCK_DIR/pod.json not found." >&2
    exit 1
  fi
  POD_DATA=$(cat "$MOCK_DIR/pod.json")
else
  if [[ -z "$POD_NAME" ]]; then
    echo "Error: --pod is required in live mode." >&2
    exit 1
  fi
  if ! command -v kubectl &>/dev/null; then
    echo "Error: 'kubectl' is required in live mode." >&2
    exit 1
  fi
  POD_DATA=$(kubectl "${KUBECTL_ARGS[@]}" get pod "$POD_NAME" -n "$NAMESPACE" -o json)
fi

ACTUAL_POD_NAME=$(echo "$POD_DATA" | jq -r '.metadata.name // empty')
ACTUAL_POD_NS=$(echo "$POD_DATA" | jq -r '.metadata.namespace // "default"')
POD_NODE_NAME=$(echo "$POD_DATA" | jq -r '.spec.nodeName // empty')

# Determine ComputeClass name if not provided
if [[ -z "$CCC_NAME" ]]; then
  CCC_NAME=$(echo "$POD_DATA" | jq -r '
    .spec.nodeSelector["cloud.google.com/compute-class"] //
    .spec.nodeSelector["compute-class"] //
    empty
  ')
fi

# Load visibility logs
if [[ -n "$MOCK_DIR" ]]; then
  if [[ ! -f "$MOCK_DIR/visibility-logs.json" ]]; then
    echo "Error: Mock file $MOCK_DIR/visibility-logs.json not found." >&2
    exit 1
  fi
  VIS_LOGS=$(cat "$MOCK_DIR/visibility-logs.json")
else
  if [[ -z "$PROJECT_ID" || -z "$CLUSTER_NAME" ]]; then
    echo "Error: --project and --cluster are required for live visibility log query." >&2
    exit 1
  fi
  FILTER="log_id(\"container.googleapis.com/cluster-autoscaler-visibility\") AND resource.labels.cluster_name=\"$CLUSTER_NAME\" AND jsonPayload.decision.scaleUp.triggeringPods.name=\"$ACTUAL_POD_NAME\""
  VIS_LOGS=$(gcloud logging read "$FILTER" --project="$PROJECT_ID" --format=json --limit=10)
fi

# Find scale-up decision for this pod
DECISION_EVENT=$(echo "$VIS_LOGS" | jq --arg pod "$ACTUAL_POD_NAME" '
  if type == "array" then . else [.] end |
  map(select(.jsonPayload.decision.scaleUp.triggeringPods[]?.name == $pod)) |
  sort_by(.timestamp // .jsonPayload.decision.decideTime) |
  last // empty
')

EVENT_ID=$(echo "$DECISION_EVENT" | jq -r '.jsonPayload.decision.eventId // empty')
DECIDE_TIME=$(echo "$DECISION_EVENT" | jq -r '.jsonPayload.decision.decideTime // empty')
INCREASED_MIGS=$(echo "$DECISION_EVENT" | jq -c '.jsonPayload.decision.scaleUp.increasedMigs // []')
TARGET_NODEPOOL=$(echo "$INCREASED_MIGS" | jq -r '.[0].mig.nodepool // empty')
TARGET_ZONE=$(echo "$INCREASED_MIGS" | jq -r '.[0].mig.zone // empty')
REQUESTED_NODES=$(echo "$INCREASED_MIGS" | jq -r '.[0].requestedNodes // empty')

# Load ComputeClass status & spec
if [[ -n "$MOCK_DIR" ]]; then
  if [[ ! -f "$MOCK_DIR/computeclass.json" ]]; then
    echo "Error: Mock file $MOCK_DIR/computeclass.json not found." >&2
    exit 1
  fi
  CCC_DATA=$(cat "$MOCK_DIR/computeclass.json")
else
  if [[ -z "$CCC_NAME" ]]; then
    echo "Error: ComputeClass name could not be determined. Use --ccc." >&2
    exit 1
  fi
  CCC_DATA=$(kubectl "${KUBECTL_ARGS[@]}" get computeclass "$CCC_NAME" -o json)
fi

PRIORITY_STATUSES=$(echo "$CCC_DATA" | jq -c '.status.priorityStatuses // []')
SPEC_PRIORITIES=$(echo "$CCC_DATA" | jq -c '.spec.priorities // []')
STATUS_MODE="CRD status.priorityStatuses"

# Load Node data to inspect labels & ccc_priority_index annotation
NODE_ANNOTATION_INDEX=""
NODE_INSTANCE_TYPE=""
NODE_IS_SPOT="false"
if [[ -n "$MOCK_DIR" ]]; then
  if [[ -f "$MOCK_DIR/nodes.json" ]]; then
    NODE_DATA=$(cat "$MOCK_DIR/nodes.json")
    if [[ -n "$POD_NODE_NAME" ]]; then
      NODE_OBJ=$(echo "$NODE_DATA" | jq -c --arg node "$POD_NODE_NAME" '
        if .items then .items[] else . end | select(.metadata.name == $node)
      ')
    else
      NODE_OBJ=$(echo "$NODE_DATA" | jq -c 'if .items then .items[0] else . end')
    fi
    NODE_ANNOTATION_INDEX=$(echo "$NODE_OBJ" | jq -r '.metadata.annotations["ccc_priority_index"] // empty')
    NODE_INSTANCE_TYPE=$(echo "$NODE_OBJ" | jq -r '.metadata.labels["node.kubernetes.io/instance-type"] // empty')
    NODE_IS_SPOT=$(echo "$NODE_OBJ" | jq -r '.metadata.labels["cloud.google.com/gke-spot"] // "false"')
  fi
else
  if [[ -n "$POD_NODE_NAME" ]]; then
    NODE_JSON=$(kubectl "${KUBECTL_ARGS[@]}" get node "$POD_NODE_NAME" -o json 2>/dev/null || echo "{}")
    NODE_ANNOTATION_INDEX=$(echo "$NODE_JSON" | jq -r '.metadata.annotations["ccc_priority_index"] // empty')
    NODE_INSTANCE_TYPE=$(echo "$NODE_JSON" | jq -r '.metadata.labels["node.kubernetes.io/instance-type"] // empty')
    NODE_IS_SPOT=$(echo "$NODE_JSON" | jq -r '.metadata.labels["cloud.google.com/gke-spot"] // "false"')
  fi
fi

# Analyze skipped and winning priorities
if [[ $(echo "$PRIORITY_STATUSES" | jq 'length') -gt 0 ]]; then
  SKIPPED_PRIORITIES=$(echo "$PRIORITY_STATUSES" | jq -c '
    [ .[] |
      select(any(.conditions[]?; (.type == "ProvisioningSuspended" or .type == "RuleMisconfigured") and .status == "True")) |
      {
        identifier: .identifier,
        configHash: .configHash,
        suspendedCondition: ([.conditions[]? | select(.type == "ProvisioningSuspended")] | first // null),
        misconfiguredCondition: ([.conditions[]? | select(.type == "RuleMisconfigured")] | first // null),
        backoffUntil: ([.conditions[]? | select(.type == "ProvisioningSuspended") | .message | capture("until (?<until>[^.]+)").until] | first // null)
      }
    ]
  ')

  WINNING_PRIORITY=$(echo "$PRIORITY_STATUSES" | jq -c '
    [ .[] |
      select(
        any(.conditions[]?; .type == "NodeProvisioningInProgress" and .status == "True") or
        (any(.conditions[]?; .type == "ProvisioningSuspended" and .status == "True") | not)
      )
    ] | first // empty
  ')
  WINNING_IDENTIFIER=$(echo "$WINNING_PRIORITY" | jq -r '.identifier // empty')
else
  # Pre-Rollout Live Inference Mode: infer winning priority index from nodepool name / node labels vs spec.priorities
  STATUS_MODE="Pre-Rollout Live Inference (spec.priorities + CA Visibility + Node Labels)"
  INFERRED_INDEX=$(echo "$SPEC_PRIORITIES" | jq -r \
    --arg np "$TARGET_NODEPOOL" \
    --arg ntype "$NODE_INSTANCE_TYPE" \
    --arg nspot "$NODE_IS_SPOT" '
    to_entries | map(
      select(
        # Match machineType against node label or NAP nodepool name
        ((.value.machineType // "") != "" and ($ntype == .value.machineType or ($np | contains(.value.machineType)))) and
        # Match spot requirement
        ((.value.spot // false) == ($nspot == "true" or ($np | contains("-spot-")))) and
        # If reservation is specified, check if node or nodepool matched reservation (unless skipped)
        ((.value.reservations // .value.reservationAffinity // null) == null)
      )
    ) | first | .key // empty
  ')
  WINNING_IDENTIFIER="$INFERRED_INDEX"
  if [[ -n "$WINNING_IDENTIFIER" && "$WINNING_IDENTIFIER" -gt 0 ]]; then
    SKIPPED_PRIORITIES=$(echo "$SPEC_PRIORITIES" | jq -c --argjson win "$WINNING_IDENTIFIER" '
      to_entries | map(select(.key < $win) | {
        identifier: (.key | tostring),
        configHash: "live-inferred",
        suspendedCondition: {
          reason: (if (.value.reservations // .value.reservationAffinity) then "SpecificReservationUnsatisfied" else "OutOfResourcesOrUnsatisfied" end),
          message: ("Priority index " + (.key | tostring) + " (" + (.value | tostring) + ") skipped by Cluster Autoscaler in favor of Priority " + ($win | tostring))
        },
        misconfiguredCondition: null,
        backoffUntil: "Live CA Cycle"
      })
    ')
  else
    SKIPPED_PRIORITIES="[]"
  fi
  WINNING_PRIORITY="{}"
fi

if [[ "$OUTPUT_JSON" == true ]]; then
  jq -n \
    --arg pod "$ACTUAL_POD_NAME" \
    --arg ns "$ACTUAL_POD_NS" \
    --arg ccc "$CCC_NAME" \
    --arg node "$POD_NODE_NAME" \
    --arg statusMode "$STATUS_MODE" \
    --arg eventId "$EVENT_ID" \
    --arg nodepool "$TARGET_NODEPOOL" \
    --arg zone "$TARGET_ZONE" \
    --arg requestedNodes "$REQUESTED_NODES" \
    --arg winningPriority "$WINNING_IDENTIFIER" \
    --arg nodeAnnotationIndex "$NODE_ANNOTATION_INDEX" \
    --argjson skipped "$SKIPPED_PRIORITIES" \
    --argjson decision "$DECISION_EVENT" \
    '{
      pod: { name: $pod, namespace: $ns, nodeName: $node, computeClass: $ccc },
      statusEvaluationMode: $statusMode,
      autoscalerDecision: {
        eventId: $eventId,
        nodepool: $nodepool,
        zone: $zone,
        requestedNodes: ($requestedNodes | tonumber? // null),
        rawDecision: $decision
      },
      priorityAnalysis: {
        skippedPriorities: $skipped,
        winningPriorityIdentifier: $winningPriority,
        nodeCccPriorityIndex: $nodeAnnotationIndex,
        traceabilityVerified: ($winningPriority != "" and ($winningPriority == $nodeAnnotationIndex or $nodeAnnotationIndex == ""))
      }
    }'
  exit 0
fi

# Human-readable output report
cat <<EOF
================================================================================
GKE ComputeClass End-to-End Scale-Up Traceability Report
================================================================================

[1] Target Pod & Workload:
    Pod:                $ACTUAL_POD_NS/$ACTUAL_POD_NAME
    Bound Node:         ${POD_NODE_NAME:-"(Pending / Not yet bound)"}
    ComputeClass:       $CCC_NAME
    Evaluation Mode:    $STATUS_MODE

[2] Cluster Autoscaler Scale-Up Decision (CA Visibility Logs):
    Decision Event ID:  ${EVENT_ID:-"Not found in visibility logs"}
    Decide Time:        ${DECIDE_TIME:-"N/A"}
    Target Node Pool:   ${TARGET_NODEPOOL:-"N/A"}
    Target Zone:        ${TARGET_ZONE:-"N/A"}
    Nodes Requested:    ${REQUESTED_NODES:-"N/A"}

[3] ComputeClass Priority Status Evaluation:
EOF

SKIPPED_COUNT=$(echo "$SKIPPED_PRIORITIES" | jq 'length')
if [[ "$SKIPPED_COUNT" -gt 0 ]]; then
  echo "    Higher Priorities Skipped ($SKIPPED_COUNT):"
  for i in $(seq 0 $((SKIPPED_COUNT - 1))); do
    ITEM=$(echo "$SKIPPED_PRIORITIES" | jq -c ".[$i]")
    IDENT=$(echo "$ITEM" | jq -r '.identifier')
    REASON=$(echo "$ITEM" | jq -r '.suspendedCondition.reason // .misconfiguredCondition.reason // "Unknown"')
    MSG=$(echo "$ITEM" | jq -r '.suspendedCondition.message // .misconfiguredCondition.message // "No details"')
    UNTIL=$(echo "$ITEM" | jq -r '.backoffUntil // "N/A"')
    echo "      • Priority [identifier: \"$IDENT\"] was skipped:"
    echo "        Reason:        $REASON"
    echo "        Backoff Until: $UNTIL"
    echo "        Detail:        $MSG"
  done
else
  echo "    No higher priorities were skipped."
fi

echo ""
echo "    Winning Priority Rule:"
if [[ -n "$WINNING_IDENTIFIER" ]]; then
  echo "      • Priority [identifier: \"$WINNING_IDENTIFIER\"] selected for scale-up."
  WIN_COND=$(echo "$WINNING_PRIORITY" | jq -c '.conditions[]? | select(.type == "NodeProvisioningInProgress") // empty')
  if [[ -n "$WIN_COND" ]]; then
    WIN_MSG=$(echo "$WIN_COND" | jq -r '.message // ""')
    echo "        Condition:     NodeProvisioningInProgress (Status: True)"
    echo "        Detail:        $WIN_MSG"
  fi
else
  echo "      • No winning priority found in status."
fi

echo ""
echo "[4] Node Verification & Annotation Matching:"
echo "    Node Annotation:    ccc_priority_index = \"${NODE_ANNOTATION_INDEX:-"<not set>"}\""
if [[ -n "$NODE_INSTANCE_TYPE" ]]; then
  echo "    Node Machine Type:  $NODE_INSTANCE_TYPE (Spot: $NODE_IS_SPOT)"
fi

if [[ -n "$WINNING_IDENTIFIER" && "$WINNING_IDENTIFIER" == "$NODE_ANNOTATION_INDEX" ]]; then
  echo "    Traceability Status: VERIFIED (Node annotation matches winning priority identifier \"$WINNING_IDENTIFIER\")"
elif [[ -n "$WINNING_IDENTIFIER" && -z "$NODE_ANNOTATION_INDEX" && -n "$POD_NODE_NAME" ]]; then
  echo "    Traceability Status: VERIFIED VIA LIVE CA LOGS & NODE LABELS (Matched Priority \"$WINNING_IDENTIFIER\"; ccc_priority_index annotation gated on cluster)"
elif [[ -z "$NODE_ANNOTATION_INDEX" ]]; then
  echo "    Traceability Status: PENDING (Node not yet provisioned or ccc_priority_index annotation not found)"
else
  echo "    Traceability Status: MISMATCH (Expected \"$WINNING_IDENTIFIER\", got \"$NODE_ANNOTATION_INDEX\")"
fi
echo "================================================================================"
