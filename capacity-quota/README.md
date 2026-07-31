# CapacityQuota scale-up limits and family fallback spillover

Define granular resource limits for the GKE cluster autoscaler using a **CapacityQuota** custom resource. This advanced example demonstrates how to cap a primary machine family (`n4`) at an 8 CPU limit while automatically spilling over excess workload demand to fallback Generation 4 machine families (`n4d` and `c4`) without leaving pods stuck in a `Pending` state.

## Prerequisites

- Requires GKE **1.36.2-gke.2064000+** on either GKE Standard or Autopilot.
- See the [authoritative CapacityQuota documentation](https://cloud.google.com/kubernetes-engine/docs/how-to/configure-capacity-quotas#select-node-labels).

## What this example shows

1. [`quota-class.yaml`](./quota-class.yaml) creates the `prefer-n4` ComputeClass with node auto-provisioning enabled (`nodePoolAutoCreation.enabled: true`) and an ordered priority ladder of Generation 4 machine families (`n4` → `n4d` → `c4`):

```yaml
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: prefer-n4
spec:
  priorities:
  - machineFamily: n4
  - machineFamily: n4d
  - machineFamily: c4
  nodePoolAutoCreation:
    enabled: true
```

2. [`quota-limit.yaml`](./quota-limit.yaml) creates the `n4-quota` CapacityQuota resource in API group `autoscaling.x-k8s.io/v1beta1`. By specifying both `cloud.google.com/compute-class: prefer-n4` and `cloud.google.com/machine-family: n4` in `matchLabels`, the 8 CPU limit restricts **only** the `n4` machine family, leaving `n4d` and `c4` uncapped:

```yaml
apiVersion: autoscaling.x-k8s.io/v1beta1
kind: CapacityQuota
metadata:
  name: n4-quota
spec:
  selector:
    matchLabels:
      cloud.google.com/compute-class: prefer-n4
      cloud.google.com/machine-family: n4
  limits:
    resources:
      cpu: 8
```

3. [`quota-deploy.yaml`](./quota-deploy.yaml) deploys 16 pause replicas requesting `500m` CPU each (8.0 CPU total) assigned to `prefer-n4`. Because nodes reserve CPU for system daemons and OS overhead, running all 16 pods requires more than 8 physical CPUs.

## Deploy

Apply the ComputeClass and CapacityQuota first, then deploy the workload:

```bash
kubectl apply -f quota-class.yaml
kubectl apply -f quota-limit.yaml
kubectl apply -f quota-deploy.yaml
```

## Observe

Wait approximately two minutes for the cluster autoscaler to evaluate the scale-up request and provision nodes.

### 1. Observe family fallback and spillover

Check where the pods are scheduled across nodes:

```bash
kubectl get pods -l app=quota-demo -o wide
kubectl get nodes -L cloud.google.com/machine-family,cloud.google.com/compute-class
```

- The cluster autoscaler first provisions **`n4`** nodes up to the `8` CPU quota threshold.
- Once `n4` reaches 8 CPU, further scale-up of `n4` is blocked by `CapacityQuota/n4-quota`.
- Because **`n4d`** and **`c4`** nodes do not carry the label `cloud.google.com/machine-family: n4`, they are exempt from `n4-quota`.
- The autoscaler automatically falls back to `n4d` (or `c4`) to provision capacity for the remaining overflow pods, ensuring all 16 pods achieve `Running` status.

### 2. Verify the scale-up block event on n4

You can verify that `n4` scale-up was capped by inspecting cluster autoscaler visibility logs or events for skipped node groups:

```
Pod didn't trigger scale-up: 1 exceeded quota: "CapacityQuota/n4-quota", resources: cpu
```

### 3. Check CapacityQuota usage and validity

Inspect the `status` field of the CapacityQuota resource:

```bash
kubectl describe capacityquota n4-quota
```

The status output confirms that `n4` usage is capped at `8` CPUs while the remaining workload runs on fallback families:

```yaml
status:
  conditions:
  - lastTransitionTime: "2026-07-31T21:30:00Z"
    message: "CapacityQuota is valid"
    reason: "Valid"
    status: "True"
    type: "cluster-autoscaler.kubernetes.io/valid"
  used:
    resources:
      cpu: 8
```

> **Note**: `status.used` reflects physical capacity after successful node scale-ups and is intended for observability. The cluster autoscaler also tracks pending nodes internally during scale-up evaluations.

## Cleanup

Remove the example resources when finished:

```bash
kubectl delete -f quota-deploy.yaml --ignore-not-found
kubectl delete -f quota-limit.yaml --ignore-not-found
kubectl delete -f quota-class.yaml --ignore-not-found
```
