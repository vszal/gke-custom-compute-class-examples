#!/usr/bin/env bash
set -euo pipefail

## CONFIGURE THESE TWO VARIABLES BEFORE RUNNING -- or export them in your shell:
##   CLUSTER_NAME=my-cluster LOCATION=us-central1 ./update-e2-node-pool.sh
CLUSTER_NAME="${CLUSTER_NAME:-}"
LOCATION="${LOCATION:-}"

if [[ -z "${CLUSTER_NAME}" || -z "${LOCATION}" ]]; then
  echo "CLUSTER_NAME and/or LOCATION is not set." >&2
  echo "Edit the top of $(basename "$0"), or run:" >&2
  echo "  CLUSTER_NAME=my-cluster LOCATION=us-central1 ./$(basename "$0")" >&2
  exit 1
fi

echo "Raising e2-4-spot-pool max-nodes to 10 in ${CLUSTER_NAME} (${LOCATION})..."

gcloud container node-pools update e2-4-spot-pool \
    --location="${LOCATION}" \
    --cluster="${CLUSTER_NAME}" \
    --enable-autoscaling \
    --max-nodes=10

echo "Done."
