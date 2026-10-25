#!/usr/bin/env bash
# Creates the GCS bucket Pod Snapshots writes to, with the settings and IAM it requires:
# hierarchical namespace, soft delete off, uniform bucket-level access, a managed folder
# for the snapshots, and access for the session pods' Kubernetes service account and the
# GKE service agent. See https://docs.cloud.google.com/kubernetes-engine/docs/how-to/agent-sandbox-pod-snapshots
set -euo pipefail

usage() {
  echo "Usage: $0 BUCKET [LOCATION]   (LOCATION defaults to the cluster's region)" >&2
}
BUCKET="${1:-}"
if [[ -z "${BUCKET}" ]]; then usage; exit 1; fi
LOCATION="${2:-${BUCKET_LOCATION:-}}"

NAMESPACE="agent-sandbox-demo"
KSA="agent-session"
FOLDER="sandboxes"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "Error: set PROJECT_ID or a default gcloud project." >&2
  exit 1
fi
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
POOL="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog"

if [[ -z "${LOCATION}" ]]; then
  # gke_<project>_<location>_<cluster> -> region
  LOCATION="$(kubectl config current-context | awk -F_ '{print $3}' | sed -E 's/-[a-z]$//')"
fi
if [[ -z "${LOCATION}" ]]; then
  echo "Error: could not infer a bucket location; pass it as the second argument." >&2
  exit 1
fi

if gcloud storage buckets describe "gs://${BUCKET}" &>/dev/null; then
  echo "Bucket gs://${BUCKET} exists; checking its settings..."
  hns="$(gcloud storage buckets describe "gs://${BUCKET}" --format='value(hierarchical_namespace.enabled)')"
  if [[ "$(tr "[:upper:]" "[:lower:]" <<<"${hns}")" != "true" ]]; then
    echo "Error: gs://${BUCKET} does not have hierarchical namespace enabled, which Pod" >&2
    echo "Snapshots requires. It cannot be enabled on an existing bucket; use a new name." >&2
    exit 1
  fi
  gcloud storage buckets update "gs://${BUCKET}" --clear-soft-delete --quiet
else
  echo "Creating gs://${BUCKET} in ${LOCATION}..."
  gcloud storage buckets create "gs://${BUCKET}" --project="${PROJECT_ID}" \
    --location="${LOCATION}" --uniform-bucket-level-access \
    --enable-hierarchical-namespace --soft-delete-duration=0d --quiet
fi
gcloud storage managed-folders create "gs://${BUCKET}/${FOLDER}/" --quiet 2>/dev/null || true

echo "Granting bucket access..."
bind() {
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="$1" --role="$2" --quiet >/dev/null
  echo "  $2 -> $1"
}
bind "principalSet://iam.googleapis.com/${POOL}/namespace/${NAMESPACE}" roles/storage.bucketViewer
bind "principal://iam.googleapis.com/${POOL}/subject/ns/${NAMESPACE}/sa/${KSA}" roles/storage.objectUser
bind "serviceAccount:service-${PROJECT_NUMBER}@container-engine-robot.iam.gserviceaccount.com" roles/storage.objectUser

echo "Done. Install with: ./install.sh --mode=snapshots --bucket=${BUCKET}"
