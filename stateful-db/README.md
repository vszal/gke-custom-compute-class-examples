# Stateful database primary: zone-pinned, reservation-first, all Gen-4

This example runs a stateful PostgreSQL **primary** on GKE with a ComputeClass
tuned for the constraints stateful databases actually have: a zonal persistent
volume, no tolerance for preemption, and a single disk generation.

## Why each design choice

### Zone-pinned for PersistentVolume affinity
A primary database keeps its data on a **zonal** Hyperdisk PV. A zonal disk can
only attach to a node **in its own zone** — if the pod lands in `us-central1-b`
while its PV lives in `us-central1-a`, the volume never attaches and the pod
hangs in `FailedAttachVolume`. So **every** priority in
[`postgres-class.yaml`](./postgres-class.yaml) is pinned to `us-central1-a`.

### Reservation-first, then On-Demand — never Spot
The priority ladder is:

1. **Specific reservation** — pre-paid capacity, highest obtainability, no preemption.
2. **On-Demand n4d**, same zone — fallback if the reservation is exhausted.
3. **On-Demand c4**, same zone — last-resort fallback.

There is deliberately **no Spot** tier: a primary cannot absorb a mid-transaction
eviction. (Spot is fine for stateless tiers or read replicas, not the primary.)

### All Gen-4, all Hyperdisk — never mix disk generations
Every priority uses `hyperdisk-balanced`, and the data PVC uses the
`hyperdisk-balanced` StorageClass. This is the trap to avoid:

> **Do not mix Gen-2 (`pd-*`) and Gen-4 (`hyperdisk-*`) disks** across the
> ComputeClass `priorities[]` or between the boot disk and the data PV. A node
> brought up on one generation can't attach a volume of the other — you get
> volume attach failures.

Keeping boot disks and the data PVC all on Hyperdisk sidesteps it entirely.

### Broadening capacity across generations with `dynamic-rwo`

This example stays on a **single** disk generation on purpose. But if you ever need
more fallback capacity and want to add a Gen-2 family (say an `n2` priority) below
the Gen-4 ones, a fixed `hyperdisk-balanced` PVC becomes exactly the trap above — a
Gen-2 node can't attach a Hyperdisk volume.

On **GKE 1.35.3-gke.1290000+** there's a supported way to span generations safely:
the built-in **`dynamic-rwo`** StorageClass.

- `type: dynamic` provisions **Persistent Disk *or* Hyperdisk per node**, matching
  whichever generation the node that schedules the pod supports.
- `use-allowed-disk-topology: "true"` makes the **Cluster Autoscaler
  disk-topology-aware**: it reads the workload's disk requirements and scales up
  **only disk-compatible nodes**, instead of bringing up an incompatible node that
  strands the pod in `FailedAttachVolume`.

Point the data PVC at `storageClassName: dynamic-rwo` and you can widen the
`priorities[]` ladder across Gen-2 and Gen-4 without attach failures. It's built in
on supported clusters — reference it by name. For reference, it resolves to:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: dynamic-rwo
provisioner: pd.csi.storage.gke.io
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
parameters:
  type: dynamic
  pd-type: pd-balanced
  hyperdisk-type: hyperdisk-balanced
  use-allowed-disk-topology: "true"
```

Two caveats:

- `dynamic` resolves to the **balanced** tiers — for `hyperdisk-extreme` / `-ml` /
  `-throughput` use a dedicated Hyperdisk class instead.
- Switching to `dynamic-rwo` only affects **newly provisioned** PVs. An existing
  Hyperdisk (or PD) volume keeps its type — migrate the data (snapshot/restore or a
  DB-level copy); PD↔Hyperdisk is not an in-place conversion.

### Kernel sysctls (applied to every priority)
`priorityDefaults.nodeSystemConfig` applies two sysctls to whichever family wins:

- `net.core.somaxconn: 1024` — a deeper listen/accept queue for databases that
  field many concurrent client connections.
- `vm.overcommit_memory: 2` — **strict** overcommit, so the kernel fails an
  oversized allocation up front instead of letting the OOM killer reap the
  postgres process mid-query.

## Architecture

```
us-central1-a (every priority pinned here)
  ┌─────────────────────────────────────────────┐
  │ Node: c4d / n4d / c4  (Gen-4)               │
  │ boot disk: hyperdisk-balanced               │
  │ sysctls: somaxconn=1024, overcommit=2       │
  │   ┌───────────────────────────────────┐     │
  │   │ Pod postgres-0 (postgres:16)      │     │
  │   │ /var/lib/postgresql/data ──┐      │     │
  │   └────────────────────────────┼──────┘     │
  └────────────────────────────────┼────────────┘
                                    ▼
              PVC 100Gi hyperdisk-balanced  →  zonal Hyperdisk PV (us-central1-a)
```

## Prerequisites

- A GKE cluster with ComputeClass support.
- A Compute Engine reservation named `postgres-primary-reservation` in
  `us-central1-a` (or edit the name in the class). List with
  `gcloud compute reservations list`.
- A `hyperdisk-balanced` StorageClass. Recent GKE versions ship one by default
  (`kubectl get storageclass`). If yours doesn't have it, create it with the
  **PD CSI driver** (not the in-tree provisioner):

  ```yaml
  apiVersion: storage.k8s.io/v1
  kind: StorageClass
  metadata:
    name: hyperdisk-balanced
  provisioner: pd.csi.storage.gke.io
  parameters:
    type: hyperdisk-balanced
  volumeBindingMode: WaitForFirstConsumer
  allowVolumeExpansion: true
  ```

  `WaitForFirstConsumer` is important — it delays PV creation until the pod is
  scheduled, so the disk is provisioned in the zone the node actually lands in.

  On **GKE 1.35.3-gke.1290000+** you can instead use the built-in **`dynamic-rwo`**
  StorageClass if your priority ladder spans disk generations — see
  [Broadening capacity across generations](#broadening-capacity-across-generations-with-dynamic-rwo).

## Deploy

```bash
kubectl apply -f postgres-class.yaml
kubectl apply -f postgres-statefulset.yaml
```

## Verify zone and machine family

```bash
# Which node did postgres-0 land on?
kubectl get pod postgres-0 -o wide

# Confirm that node's zone, machine family, and exact shape
kubectl get node <node-name> \
  -L topology.kubernetes.io/zone,cloud.google.com/machine-family,node.kubernetes.io/instance-type
```

The zone should be `us-central1-a` and the instance type a `c4d-`, `n4d-`, or
`c4-` shape. Then confirm the data volume bound to Hyperdisk:

```bash
kubectl get pvc postgres-data-postgres-0
# STORAGECLASS column should read hyperdisk-balanced
```

## Common pitfalls

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Pod `Pending`, no node | reservation name mismatch / exhausted | match the name to `gcloud compute reservations list`; check capacity |
| `FailedAttachVolume` | node and PV in different zones | keep every priority pinned to one zone; `WaitForFirstConsumer` on the StorageClass |
| volume attach error across generations | mixed `pd-*` and `hyperdisk-*` | use `hyperdisk-balanced` everywhere — boot disks **and** the data PVC |

## Production notes

- Replace the demo `POSTGRES_PASSWORD` env value with a `secretKeyRef`.
- Add `resources.limits` alongside `requests`.
- PVCs aren't backed up automatically — wire up GKE Backup or your own.
