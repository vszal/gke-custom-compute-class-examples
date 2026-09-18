#!/usr/bin/env bash
# ==============================================================================
# test-scripts.sh
# Automated regression test suite for ComputeClass observability CLI scripts.
# Tests trace-pod-scaleup.sh and monitor-hard-stockouts.sh against mock datasets.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRACE_SCRIPT="$SCRIPT_DIR/trace-pod-scaleup.sh"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor-hard-stockouts.sh"
VERIFY_MINCAP_SCRIPT="$SCRIPT_DIR/verify-minimum-capacity.sh"
TRACE_MOCK_DIR="$SCRIPT_DIR/mock-data/scenario-traceability"
STOCKOUT_MOCK_DIR="$SCRIPT_DIR/mock-data/scenario-hard-stockout"
MINCAP_MOCK_DIR="$SCRIPT_DIR/mock-data/scenario-min-capacity"

TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

assert_eq() {
  local test_name="$1"
  local actual="$2"
  local expected="$3"
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  if [[ "$actual" == "$expected" ]]; then
    echo "  [PASS] $test_name"
    PASSED_TESTS=$((PASSED_TESTS + 1))
  else
    echo "  [FAIL] $test_name: expected '$expected', got '$actual'"
    FAILED_TESTS=$((FAILED_TESTS + 1))
  fi
}

assert_contains() {
  local test_name="$1"
  local haystack="$2"
  local needle="$3"
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "  [PASS] $test_name"
    PASSED_TESTS=$((PASSED_TESTS + 1))
  else
    echo "  [FAIL] $test_name: string did not contain '$needle'"
    FAILED_TESTS=$((FAILED_TESTS + 1))
  fi
}

echo "================================================================================"
echo "Starting GKE ComputeClass Observability Script Test Suite"
echo "================================================================================"

# ------------------------------------------------------------------------------
# Test Suite 1: trace-pod-scaleup.sh (Human Report Mode)
# ------------------------------------------------------------------------------
echo ""
echo "Suite 1: trace-pod-scaleup.sh --mock (Text Report)"
TRACE_TEXT_OUT=$("$TRACE_SCRIPT" --mock "$TRACE_MOCK_DIR")

assert_contains "Trace output contains Pod info" "$TRACE_TEXT_OUT" "ml-workloads/worker-job-8x"
assert_contains "Trace output contains Event ID" "$TRACE_TEXT_OUT" "scaleup-evt-c4a2e1-88f1"
assert_contains "Trace output identifies skipped Priority 0" "$TRACE_TEXT_OUT" "Priority [identifier: \"0\"] was skipped"
assert_contains "Trace output shows OutOfResources backoff reason" "$TRACE_TEXT_OUT" "Reason:        OutOfResources"
assert_contains "Trace output extracts UTC backoff timestamp" "$TRACE_TEXT_OUT" "Backoff Until: 2026-09-17 20:45:00 UTC"
assert_contains "Trace output selects winning Priority 1" "$TRACE_TEXT_OUT" "Priority [identifier: \"1\"] selected for scale-up"
assert_contains "Trace output verifies ccc_priority_index annotation" "$TRACE_TEXT_OUT" "ccc_priority_index = \"1\""
assert_contains "Trace status is VERIFIED" "$TRACE_TEXT_OUT" "Traceability Status: VERIFIED"

# ------------------------------------------------------------------------------
# Test Suite 2: trace-pod-scaleup.sh (JSON Mode)
# ------------------------------------------------------------------------------
echo ""
echo "Suite 2: trace-pod-scaleup.sh --mock --json"
TRACE_JSON_OUT=$("$TRACE_SCRIPT" --mock "$TRACE_MOCK_DIR" --json)

assert_eq "JSON field traceabilityVerified is true" \
  "$(echo "$TRACE_JSON_OUT" | jq -r '.priorityAnalysis.traceabilityVerified')" "true"

assert_eq "JSON winningPriorityIdentifier is '1'" \
  "$(echo "$TRACE_JSON_OUT" | jq -r '.priorityAnalysis.winningPriorityIdentifier')" "1"

assert_eq "JSON nodeCccPriorityIndex is '1'" \
  "$(echo "$TRACE_JSON_OUT" | jq -r '.priorityAnalysis.nodeCccPriorityIndex')" "1"

assert_eq "JSON skipped priority count is 1" \
  "$(echo "$TRACE_JSON_OUT" | jq -r '.priorityAnalysis.skippedPriorities | length')" "1"

assert_eq "JSON skipped priority 0 reason is OutOfResources" \
  "$(echo "$TRACE_JSON_OUT" | jq -r '.priorityAnalysis.skippedPriorities[0].suspendedCondition.reason')" "OutOfResources"

assert_eq "JSON skipped priority 0 backoffUntil matches" \
  "$(echo "$TRACE_JSON_OUT" | jq -r '.priorityAnalysis.skippedPriorities[0].backoffUntil')" "2026-09-17 20:45:00 UTC"

# ------------------------------------------------------------------------------
# Test Suite 3: monitor-hard-stockouts.sh (Critical Stockout Scenario)
# ------------------------------------------------------------------------------
echo ""
echo "Suite 3: monitor-hard-stockouts.sh --mock (Hard Stockout Text Report)"
MONITOR_TEXT_OUT=$("$MONITOR_SCRIPT" --mock "$STOCKOUT_MOCK_DIR")

assert_contains "Hard stockout status is CRITICAL" "$MONITOR_TEXT_OUT" "STATUS:                     CRITICAL - HARD STOCKOUT ACTIVE"
assert_contains "Calculated earliest backoff expiry" "$MONITOR_TEXT_OUT" "Earliest Backoff Expiry:    2026-09-17 20:42:00 UTC"
assert_contains "Reports all 2 priorities suspended" "$MONITOR_TEXT_OUT" "Suspended Rules (Cooldown): 2"
assert_contains "Reports 12 unsatisfied pods" "$MONITOR_TEXT_OUT" "Total Unsatisfied Pods:     12"
assert_contains "Reports CA Visibility reason" "$MONITOR_TEXT_OUT" "No Scale-Up Reason:         no.scale.up.in.backoff"
assert_contains "Reports Warning FailedScaleUp event" "$MONITOR_TEXT_OUT" "GCE out of resources"

# ------------------------------------------------------------------------------
# Test Suite 4: monitor-hard-stockouts.sh (Critical Stockout JSON Mode)
# ------------------------------------------------------------------------------
echo ""
echo "Suite 4: monitor-hard-stockouts.sh --mock --json (Hard Stockout)"
MONITOR_JSON_OUT=$("$MONITOR_SCRIPT" --mock "$STOCKOUT_MOCK_DIR" --json)

assert_eq "JSON isHardStockout is true" \
  "$(echo "$MONITOR_JSON_OUT" | jq -r '.evaluation.isHardStockout')" "true"

assert_eq "JSON severity is CRITICAL" \
  "$(echo "$MONITOR_JSON_OUT" | jq -r '.evaluation.severity')" "CRITICAL"

assert_eq "JSON suspendedPriorities count is 2" \
  "$(echo "$MONITOR_JSON_OUT" | jq -r '.evaluation.suspendedPriorities')" "2"

assert_eq "JSON activePriorities count is 0" \
  "$(echo "$MONITOR_JSON_OUT" | jq -r '.evaluation.activePriorities')" "0"

assert_eq "JSON earliestBackoffExpiration is 2026-09-17 20:42:00 UTC" \
  "$(echo "$MONITOR_JSON_OUT" | jq -r '.evaluation.earliestBackoffExpiration')" "2026-09-17 20:42:00 UTC"

assert_eq "JSON totalUnhandledPods is 12" \
  "$(echo "$MONITOR_JSON_OUT" | jq -r '.workloadImpact.totalUnhandledPods')" "12"

assert_eq "JSON caVisibilityReason is no.scale.up.in.backoff" \
  "$(echo "$MONITOR_JSON_OUT" | jq -r '.workloadImpact.caVisibilityReason')" "no.scale.up.in.backoff"

# ------------------------------------------------------------------------------
# Test Suite 5: monitor-hard-stockouts.sh (Healthy/Partial Scenario)
# ------------------------------------------------------------------------------
echo ""
echo "Suite 5: monitor-hard-stockouts.sh against scenario-traceability (Partial/Healthy)"
HEALTHY_JSON_OUT=$("$MONITOR_SCRIPT" --mock "$TRACE_MOCK_DIR" --json)

assert_eq "Healthy scenario isHardStockout is false" \
  "$(echo "$HEALTHY_JSON_OUT" | jq -r '.evaluation.isHardStockout')" "false"

assert_eq "Healthy scenario severity is OK" \
  "$(echo "$HEALTHY_JSON_OUT" | jq -r '.evaluation.severity')" "OK"

assert_eq "Healthy scenario activePriorities is 1" \
  "$(echo "$HEALTHY_JSON_OUT" | jq -r '.evaluation.activePriorities')" "1"

# ------------------------------------------------------------------------------
# Test Suite 6: verify-minimum-capacity.sh (Text Report)
# ------------------------------------------------------------------------------
echo ""
echo "Suite 6: verify-minimum-capacity.sh --mock (Shortfall & Reservation Audit Text Report)"
MINCAP_TEXT_OUT=$("$VERIFY_MINCAP_SCRIPT" --ccc meta-tpu-serving-ccc --mock "$MINCAP_MOCK_DIR")

assert_contains "Reports SHORTFALL_UNFULFILLED status" "$MINCAP_TEXT_OUT" "Fulfillment Status:    SHORTFALL_UNFULFILLED"
assert_contains "Reports effective node floor of 4" "$MINCAP_TEXT_OUT" "Effective Node Floor:  4 node(s) required"
assert_contains "Reports Priority 0 reservation name" "$MINCAP_TEXT_OUT" "Reservation=gsc-tpu-cube-res-a"
assert_contains "Reports CRD Condition MinCapacityProvisioned" "$MINCAP_TEXT_OUT" "CRD Condition: MinCapacityProvisioned=True (Reason: ProvisioningComplete)"
assert_contains "Reports synthetic scale-up pod" "$MINCAP_TEXT_OUT" "min-capacity-priority-pod-meta-tpu-serving-ccc-1-0"
assert_contains "Reports synthetic unhandled pod group" "$MINCAP_TEXT_OUT" "min-capacity-priority-pod-meta-tpu-serving-ccc-0-1"
assert_contains "Reports expected WAI floor protection" "$MINCAP_TEXT_OUT" "[EXPECTED WAI] Nodes protected from scale-down by minimumCapacity floor"

# ------------------------------------------------------------------------------
# Test Suite 7: verify-minimum-capacity.sh (JSON Mode)
# ------------------------------------------------------------------------------
echo ""
echo "Suite 7: verify-minimum-capacity.sh --mock --json (Shortfall & Reservation Audit JSON)"
MINCAP_JSON_OUT=$("$VERIFY_MINCAP_SCRIPT" --ccc meta-tpu-serving-ccc --mock "$MINCAP_MOCK_DIR" --json)

assert_eq "JSON status is SHORTFALL_UNFULFILLED" \
  "$(echo "$MINCAP_JSON_OUT" | jq -r '.status')" "SHORTFALL_UNFULFILLED"

assert_eq "JSON effectiveTargetNodeCount is 4" \
  "$(echo "$MINCAP_JSON_OUT" | jq -r '.effectiveTargetNodeCount')" "4"

assert_eq "JSON globalShortfall is 2" \
  "$(echo "$MINCAP_JSON_OUT" | jq -r '.globalShortfall')" "2"

assert_eq "JSON Priority 0 shortfall is 2" \
  "$(echo "$MINCAP_JSON_OUT" | jq -r '.priorityBreakdown[0].shortfall')" "2"

# ------------------------------------------------------------------------------
# Test Summary
# ------------------------------------------------------------------------------
echo ""
echo "================================================================================"
echo "Test Execution Summary: $PASSED_TESTS / $TOTAL_TESTS tests passed"
echo "================================================================================"

if [[ "$FAILED_TESTS" -eq 0 ]]; then
  echo "All tests passed with 100% success rate!"
  exit 0
else
  echo "Error: $FAILED_TESTS tests failed!" >&2
  exit 1
fi
