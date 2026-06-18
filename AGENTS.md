# GKE ComputeClass Examples - Validation Guide

This guide defines the standard, repeatable validation routine for GKE ComputeClass manifests and workload deployments in this repository.

## Pre-requisites & Core Mandate

To validate workload templates successfully using `kubectl apply --dry-run=server`, the GKE validating admission webhooks (like GKE Warden) must be able to dynamically resolve referenced ComputeClasses. Therefore, **you must apply the ComputeClasses to the cluster before validating the workload templates**, and then remove them during cleanup.

---

## The Standard Validation Routine

Follow these 4 steps in order to validate changes or new examples:

### 1. Apply All ComputeClasses
Apply all ComputeClass definitions across the repository:
```bash
for f in $(find . -maxdepth 2 -name "*class.yaml" -o -name "*class*.yaml"); do
  echo "Applying ComputeClass: $f"
  kubectl apply -f "$f"
done
```

### 2. Set Up Custom Namespace (If Required)
Some examples (such as `default-class-namespace`) require specific namespaces and namespace labels to be configured in order to validate:
```bash
kubectl create namespace team-a --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace team-a cloud.google.com/default-compute-class=team-default --overwrite

# restrict-usage: governed namespace that allowed-deploy.yaml targets
kubectl create namespace restricted-demo --dry-run=client -o yaml | kubectl apply -f -
```

### 3. Run Server Dry-Runs on Workloads
Execute a server-side dry-run validation on all workload definitions (Deployments, Jobs, StatefulSets, etc.):
```bash
for folder in *; do
  if [ -d "$folder" ] && [ "$folder" != ".git" ] && [ "$folder" != ".agents" ]; then
    for f in "$folder"/*deploy.yaml "$folder"/*deploy*.yaml "$folder"/*job.yaml "$folder"/*statefulset.yaml; do
      if [ -f "$f" ]; then
        echo "Validating workload: $f"
        kubectl apply -f "$f" --dry-run=server
      fi
    done
  fi
done
```

### 4. Cleanup Cluster Resources
Restore the cluster to its pristine state by deleting the temporary ComputeClasses and custom namespaces:
```bash
for f in $(find . -maxdepth 2 -name "*class.yaml" -o -name "*class*.yaml"); do
  echo "Cleaning up ComputeClass: $f"
  kubectl delete -f "$f" --ignore-not-found
done

# Delete custom namespaces
kubectl delete namespace team-a --ignore-not-found
kubectl delete namespace restricted-demo --ignore-not-found
```

> **Note on `restrict-usage`:** its `rbac-editor.yaml` and `restrict-usage-vap.yaml`
> are not `*class.yaml`/workload files, so they're outside the loops above — apply and
> clean them up per that folder's README when validating the deny behavior.

---

## Documenting Results
Upon completion of the validation routine, summarize the outcomes in `validation-results.md` matching the established table structure:
- Retrieve the current git commit shorthash (`git rev-parse --short HEAD`).
- Record the date and status (`PASS ✅` or `PASS ⚠️` / `FAIL ❌`).
- Commit and push `validation-results.md` as part of the routine so the latest results are recorded in the repo.
