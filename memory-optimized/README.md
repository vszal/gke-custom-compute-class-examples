# Memory-optimized nodes (m-series)

Targets the **memory-optimized m-series**, which carries far more memory per vCPU
than general-purpose `c4`/`n4`. RAM-bound workloads — in-memory databases and
caches, large analytics, SAP HANA — would otherwise force you to over-provision
vCPU just to reach the memory they need.

The deploy requests **128 GiB against 2 vCPU**: a ratio a general-purpose family
can't satisfy without a huge, wasteful node, but a natural fit for the m-series.

- `machineFamily: m4` (current gen) with `m3` as fallback.
- `whenUnsatisfiable: DoNotScaleUp` is deliberate — if no memory-optimized node is
  available, the pod stays **Pending** rather than landing on a general-purpose
  node that can't hold the working set.

> **Availability caveat.** m-series — and especially `x4` (the largest, for SAP
> HANA) — has narrower regional availability and is often reservation-gated.
> Confirm the family exists in your region and align it with your reservations/committed use discounts.

## Deploy & observe

```bash
kubectl apply -f mem-class.yaml
kubectl apply -f mem-deploy.yaml

kubectl get pods -o wide -l app=in-memory-cache
kubectl get nodes -L cloud.google.com/compute-class,cloud.google.com/machine-family,node.kubernetes.io/instance-type
```
