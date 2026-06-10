# Balanced multi-zone scale-up across zonal reservations

This example shows how to spread a reserved-capacity workload evenly across
three zones — at **both** the node layer and the pod layer — and walks through
the schema traps that bite people the first time they wire reservations into a
ComputeClass.

## "Balanced" is two independent layers

When someone says they want a workload "balanced" or "spread evenly" across
zones, they usually mean one or both of these — and they are configured in
different places:

| Layer | What it spreads | Where you set it | Knob |
|-------|-----------------|------------------|------|
| **Infrastructure / node** | Node scale-up across zones | ComputeClass | `location.locationPolicy: BALANCED` |
| **Workload / pod** | Pods across zones | Pod / Deployment | `topologySpreadConstraints` |

- `locationPolicy: BALANCED` makes the autoscaler spread **node** scale-up
  roughly evenly across zones. It's **best-effort**: if one zone is short on
  capacity, GKE still scales the others. (`ANY` instead packs one zone.)
- BALANCED does **not** guarantee even **pod** placement. The scheduler can
  still pile pods onto whichever nodes exist first. To pin pods 3/3/3 you need
  `topologySpreadConstraints` on the **Pod**, not the ComputeClass.

For real high availability you typically want both, which is why this example
ships both files.

## The schema trap: `location.zones` vs `affinity: Specific`

You **cannot** combine `location.zones` with `reservations.affinity: Specific`.
GKE rejects it with:

```
location config with specific reservations enabled
```

So the zone list lives **only** in `reservations.specific[].zones`, and the
`location` block keeps `locationPolicy` only:

```yaml
location:
  locationPolicy: BALANCED   # policy only — no zones here
reservations:
  affinity: Specific
  specific:
  - name: <RESERVATION-32-ZONE-A>
    zones: ['us-central1-a']  # zones come from the reservation entries
```

## One priority per machine *size*, not per zone

Tempting but wrong:

```yaml
# ANTI-PATTERN — per-zone priorities drain zone-a first
priorities:
- machineType: n4-standard-32
  location: { zones: ['us-central1-a'] }
- machineType: n4-standard-32
  location: { zones: ['us-central1-b'] }
- machineType: n4-standard-32
  location: { zones: ['us-central1-c'] }
```

Priorities are evaluated **sequentially**, so this exhausts zone-a before it
ever tries b or c. Instead use **one priority per machine size** that names all
three zonal reservations together, and let `BALANCED` spread the scale-up:

```yaml
priorities:
- machineType: n4-standard-32
  location: { locationPolicy: BALANCED }
  reservations:
    affinity: Specific
    specific:
    - name: <RESERVATION-32-ZONE-A>
      zones: ['us-central1-a']
    - name: <RESERVATION-32-ZONE-B>
      zones: ['us-central1-b']
    - name: <RESERVATION-32-ZONE-C>
      zones: ['us-central1-c']
```

## Why `Specific` and not `AnyBestEffort`

- **`Specific`** consumes only the named reservations. If they're exhausted,
  ComputeClass falls through to the next priority (and here, with
  `whenUnsatisfiable: DoNotScaleUp`, ultimately leaves pods Pending rather than
  spilling to unreserved capacity).
- **`AnyBestEffort`** (and `Automatic`) fall back to On-Demand at the GCE layer,
  **silently skipping your lower ComputeClass priorities** — so a Spot or
  cheaper fallback you defined would never fire. Always use `Specific` when you
  want ComputeClass fallback to behave predictably.

## Requirements

- A GKE cluster (the balanced-reservations pattern works broadly; no
  `priorityScore` is needed here).
- Three **zonal** reservations per machine size, one in each of
  `us-central1-a/b/c`. List them with:
  ```bash
  gcloud compute reservations list
  ```

## Deploy

1. Edit [`balanced-class.yaml`](./balanced-class.yaml) and replace the
   `<RESERVATION-*>` placeholders with your real reservation names.
2. Apply:
   ```bash
   kubectl apply -f balanced-class.yaml
   kubectl apply -f balanced-deploy.yaml
   ```

## Verify even spread

```bash
# Node layer — nodes should appear across all three zones
kubectl get nodes -L topology.kubernetes.io/zone

# Pod layer — count pods per zone; expect 3 / 3 / 3
kubectl get pods -o wide -l app=balanced-app
```

If pods sit `Pending`, `kubectl describe pod <name>` — the usual causes are a
reservation name mismatch (check `gcloud compute reservations list`), exhausted
reservation capacity, or the spread constraint having nowhere balanced to land.

## Cleanup

```bash
kubectl delete -f balanced-deploy.yaml
kubectl delete -f balanced-class.yaml
```
