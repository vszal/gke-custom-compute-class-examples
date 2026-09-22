#!/usr/bin/env bash
# Prints node pools, nodes, and pods for the target cluster.
# Intended for `watch ./status.sh` while an example scales up or down.
# No `set -e`: one failing section should not abort the rest of the report.
set -uo pipefail

## CONFIGURE THESE TWO VARIABLES BEFORE RUNNING -- or export them in your shell:
##   CLUSTER_NAME=my-cluster LOCATION=us-central1 ./status.sh
CLUSTER_NAME="${CLUSTER_NAME:-}"
LOCATION="${LOCATION:-}"

if [[ -z "${CLUSTER_NAME}" || -z "${LOCATION}" ]]; then
  echo "CLUSTER_NAME and/or LOCATION is not set." >&2
  echo "Edit the top of $(basename "$0"), or run:" >&2
  echo "  CLUSTER_NAME=my-cluster LOCATION=us-central1 ./$(basename "$0")" >&2
  echo "  (or: CLUSTER_NAME=my-cluster LOCATION=us-central1 watch ./$(basename "$0"))" >&2
  exit 1
fi

echo 'Node pools'
gcloud container node-pools list --cluster "${CLUSTER_NAME}" --location "${LOCATION}"
echo
echo 'Nodes'
kubectl get nodes -o=custom-columns=NAME:.metadata.name,INSTANCE-TYPE:".metadata.labels.node\.kubernetes\.io/instance-type",SPOT:".metadata.labels.cloud\.google\.com/gke-spot",COMPUTE-CLASS:".metadata.labels.cloud\.google\.com/compute-class"
echo
echo 'Pods'
kubectl get pods -o=wide
