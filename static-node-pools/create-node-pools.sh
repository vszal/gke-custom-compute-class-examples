#!/usr/bin/env bash
set -euo pipefail

## CONFIGURE THESE TWO VARIABLES BEFORE RUNNING -- or export them in your shell:
##   CLUSTER_NAME=my-cluster LOCATION=us-central1 ./create-node-pools.sh
CLUSTER_NAME="${CLUSTER_NAME:-}"
LOCATION="${LOCATION:-}"

if [[ -z "${CLUSTER_NAME}" || -z "${LOCATION}" ]]; then
  echo "CLUSTER_NAME and/or LOCATION is not set." >&2
  echo "Edit the top of $(basename "$0"), or run:" >&2
  echo "  CLUSTER_NAME=my-cluster LOCATION=us-central1 ./$(basename "$0")" >&2
  exit 1
fi

echo "Creating node pools in ${CLUSTER_NAME} (${LOCATION})..."

# The pool names below are referenced verbatim by static-pools-class.yaml.
# If you rename one here, rename it there too -- GKE does NOT validate that a
# name in `nodepools:` exists, so a typo yields a class that applies cleanly
# and then never provisions anything.

# Create e2-standard-4 spot pool
gcloud container node-pools create e2-4-spot-pool \
    --location="${LOCATION}" \
    --cluster="${CLUSTER_NAME}" \
    --machine-type=e2-standard-4 \
    --spot \
    --enable-autoscaling \
    --max-nodes=1 \
    --node-labels="cloud.google.com/compute-class=cost-optimized" \
    --node-taints="cloud.google.com/compute-class=cost-optimized:NoSchedule"

# Create n2-standard-4 spot pool
gcloud container node-pools create n2-4-spot-pool \
    --location="${LOCATION}" \
    --cluster="${CLUSTER_NAME}" \
    --machine-type=n2-standard-4 \
    --spot \
    --enable-autoscaling \
    --max-nodes=2 \
    --node-labels="cloud.google.com/compute-class=cost-optimized" \
    --node-taints="cloud.google.com/compute-class=cost-optimized:NoSchedule"

# Create n2d-standard-4 spot pool
gcloud container node-pools create n2d-4-spot-pool \
    --location="${LOCATION}" \
    --cluster="${CLUSTER_NAME}" \
    --machine-type=n2d-standard-4 \
    --spot \
    --enable-autoscaling \
    --max-nodes=5 \
    --node-labels="cloud.google.com/compute-class=cost-optimized" \
    --node-taints="cloud.google.com/compute-class=cost-optimized:NoSchedule"

echo "Done."
