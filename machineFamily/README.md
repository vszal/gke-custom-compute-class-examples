# machineFamily priority rule

The simplest ComputeClass: rank whole **machine families** in preference order and
let GKE provision the first one that has capacity.

## What this example shows

[`family-class.yaml`](./family-class.yaml) prefers the **c4** family, falls back to
**c3d**, and requires nodes with **at least 4 cores**:

```yaml
priorities:
- machineFamily: c4
  minCores: 4
- machineFamily: c3d
  minCores: 4
```

- `machineFamily` selects a family (here Gen-4 `c4`, then `c3d`) without pinning an
  exact shape — node auto-provisioning sizes the node to your Pods.
- `minCores` floors the node size.
- `activeMigration.optimizeRulePriority: true` lets GKE **migrate workloads up** to a
  higher-priority family when its capacity returns (e.g. c3d → c4).
- `nodePoolAutoCreation.enabled: true` lets GKE create node pools on demand
  (node auto-provisioning).

## Deploy

```bash
kubectl apply -f family-class.yaml
kubectl apply -f family-deploy.yaml   # 10 pause replicas, 700m CPU / 1Gi each
```

## Observe

```bash
watch ../status.sh    # after configuring status.sh (see the top-level README)

# or surface the family + exact shape + class directly:
kubectl get nodes -L cloud.google.com/machine-family,node.kubernetes.io/instance-type,cloud.google.com/compute-class
```

You should see **c4** nodes when the family has capacity, otherwise **c3d**.
