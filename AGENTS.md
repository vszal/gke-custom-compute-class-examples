# GKE ComputeClass Examples - Validation Guide

This guide defines the standard, repeatable validation routine for GKE ComputeClass manifests and workload deployments in this repository.

## Git repo requirements

Do not push any Example changes to the repo until validated using this process.

## Pre-requisites & Core Mandate

To validate workload templates successfully using `kubectl apply --dry-run=server`, the GKE validating admission webhooks (like GKE Warden) must be able to dynamically resolve referenced ComputeClasses. Therefore, **you must apply the ComputeClasses to the cluster before validating the workload templates**, and then remove them during cleanup.

Try the local kube context before assuming no GKE cluster is available.

Do not leave permanent objects running on the cluster. If creation of objects on the cluster is necessary to execute tests, use naming conventions to avoid conflicts (long random strings etc).

## Delegate to weaker models

Whenever possible, delegate validations to weaker models (local > cloud)

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

# restrict-usage admission policy, binding and RBAC (cluster-scoped, outlive the namespace)
kubectl delete -f restrict-usage/restrict-usage-vap.yaml -f restrict-usage/rbac-editor.yaml --ignore-not-found

# Delete custom namespaces
kubectl delete namespace team-a --ignore-not-found
kubectl delete namespace restricted-demo --ignore-not-found
kubectl delete namespace ccc-observability --ignore-not-found
```

> **Note on specialized templates (`restrict-usage`, `agent-sandbox`, `agent-substrate`):**
> - `restrict-usage`: `rbac-editor.yaml` and `restrict-usage-vap.yaml` validate RBAC and admission denial.
> - `agent-sandbox`: `agent-sandbox-template.yaml` validates `SandboxTemplate`, `SandboxWarmPool`, and `SandboxClaim` (gVisor and microVM) when the GKE Agent Sandbox addon is enabled. `agent-sandbox-gvisor-snapshots.yaml` and `agent-sandbox-gvisor-rootfs-snapshots.yaml` additionally need the Pod Snapshots CRDs (`--enable-pod-snapshots`).
> - `agent-substrate`: `agent-substrate-pool.yaml` validates `WorkerPool` when Agent Substrate CRDs are installed (it needs the `agents` namespace). `actor-template.yaml.tmpl` is protojson for `kubectl ate create actor-template`, not a Kubernetes object, and has no server dry-run.
> Each folder also includes a standard `*deploy.yaml` tested by the automated loop above.

---

## Documenting Results
Upon completion of the validation routine, summarize the outcomes in `validation-results.md` matching the established table structure:
- Retrieve the current git commit shorthash (`git rev-parse --short HEAD`).
- Record the date and status (`PASS ✅` or `PASS ⚠️` / `FAIL ❌`).
- Commit and push `validation-results.md` as part of the routine so the latest results are recorded in the repo.
