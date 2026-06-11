# machineType + Spot fallback

Pin **exact machine shapes** and combine them with the `spot` flag to build a
cheap-first, on-demand-backstop ladder.

## What this example shows

[`type-class.yaml`](./type-class.yaml) prefers Spot capacity, then falls back to
On-Demand of the same shape:

```yaml
priorities:
- machineType: c3-standard-8-lssd     # Spot, first choice (cheapest)
  spot: true
- machineType: c3d-standard-8-lssd    # Spot, second family
  spot: true
- machineType: c3-standard-8-lssd     # On-Demand backstop (never preempted)
  spot: false
whenUnsatisfiable: DoNotScaleUp
```

- `machineType` pins the **exact shape** — contrast with `machineFamily`, which only
  picks a family and lets node auto-provisioning choose the size. These `-lssd` shapes ship with local
  SSD attached.
- `spot: true` requests Spot VMs (cheap, preemptible). The final `spot: false` rule
  guarantees a landing spot when Spot capacity is unavailable.
- `whenUnsatisfiable: DoNotScaleUp` tells GKE **not** to create nodes if none of the
  priorities can be met — Pods stay Pending rather than provisioning something
  off-list.

## Deploy

```bash
kubectl apply -f type-class.yaml
kubectl apply -f type-deploy.yaml     # 10 pause replicas
```

## Observe

```bash
watch ../status.sh    # the SPOT column shows which pods landed on Spot vs On-Demand
```
