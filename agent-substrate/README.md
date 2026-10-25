# Agent Substrate on ComputeClasses (microVM and gVisor)

This example provisions worker nodes for [Agent Substrate](https://docs.cloud.google.com/kubernetes-engine/ai-ml/install-overview-substrate)
with ComputeClasses, for both of Substrate's sandbox runtimes: microVM (Cloud Hypervisor on
nested KVM) and gVisor (runsc).

Substrate suspends agents ("actors") to memory snapshots and resumes them later, often on a
different worker. That is what makes ComputeClass design different here. A ladder that is
fine for stateless pods, such as `c4` → `n4` → `c3`, can make resumed actors crash. **With
Substrate, designing the class means designing which hardware every snapshot may be
restored on.**

> **Requires GKE Standard 1.37.0-gke.4713000+** with Agent Substrate installed (see
> [Prerequisites](#prerequisites)). The manifests pin Substrate `v0.4.0-gke.0` images by
> digest. Match `ate.dev/substrate-version` in the class file to your install.

## Prerequisites

Versions and setup steps change, so treat the linked GKE pages as the source of truth.

| Dependency | Needed for | Minimum GKE | How to set it up |
|---|---|---|---|
| GKE Standard cluster (Autopilot isn't supported), Workload Identity Federation, beta APIs `PodCertificateRequest` and `ClusterTrustBundle` | Agent Substrate | 1.37 (1.36 with the beta APIs enabled at cluster creation) | [About Agent Substrate on GKE](https://docs.cloud.google.com/kubernetes-engine/ai-ml/about-agent-substrate) |
| Agent Substrate, its snapshot bucket, and the `kubectl ate` plugin | Everything | — | [Install Agent Substrate on GKE](https://docs.cloud.google.com/kubernetes-engine/ai-ml/install-overview-substrate) ([substrate-gke](https://github.com/ai-on-gke/substrate-gke) installer). The `kubectl ate` plugin is not included: build it with `go install ./cmd/kubectl-ate` from a clone of [agent-substrate/substrate](https://github.com/agent-substrate/substrate) at the tag matching your cluster (`v0.4.0` for `v0.4.0-gke.0`; a newer plugin fails with protobuf errors; Go 1.27+) |
| microVM runtime dependencies (the `microvm` SandboxConfig) | microVM lineages | — | The microVM section of [Install Agent Substrate on GKE](https://docs.cloud.google.com/kubernetes-engine/ai-ml/install-overview-substrate) |
| Nested virtualization through a ComputeClass | microVM classes | 1.37.0-gke.4713000 | [Use nested VMs with GKE clusters](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nested-virtualization). Pick series with nested virtualization from the [machine series comparison](https://docs.cloud.google.com/compute/docs/machine-resource). |
| Node pool auto-creation by ComputeClasses | All classes | 1.33.3-gke.1136000 (earlier versions also need cluster-level node auto-provisioning) | [About custom ComputeClasses](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-custom-compute-classes) |

Substrate needs no GKE Sandbox setup: it ships its own runtimes (see
[below](#configure-the-class-for-substrate-not-for-gke-sandbox)).

## The constraint: snapshots restore only on compatible CPUs

A **lineage** is an ActorTemplate, its golden snapshot, and every actor snapshot that
descends from it. Substrate builds the golden snapshot once, when the template is created,
on one worker that matches the template's `workerSelector`. Every actor starts from that
snapshot, and every suspend writes a new one. **Every worker in a lineage must be able to
restore every snapshot in it.**

Substrate's scheduler doesn't check this. It hands an actor to any worker that matches
`workerSelector`. An incompatible host doesn't fail at admission or scheduling. The actor
crashes later, when it is restored.

What a lineage can safely vary:

| Runtime | Machine size (`-8` → `-16` → `-4`) | Zone | Machine family |
|---|---|---|---|
| microVM | ✅ | ✅ | ❌ one family per lineage |
| gVisor | ✅ | ⚠️ only with a golden built on the right host | ❌ one family per lineage (Intel C3 / N4 / C4 can restore with a C3 golden, but GKE doesn't support it) |

[GKE's Agent Substrate docs](https://docs.cloud.google.com/kubernetes-engine/ai-ml/about-agent-substrate)
also require each ActorTemplate to select **a single VM type**, so the supported layout is
one machine family per lineage for both runtimes.

### gVisor: the golden host fixes the CPU feature set

gVisor records the host's CPU feature set when it builds the golden snapshot. Later
suspends, even on more capable hosts, don't change it. Every host in the lineage needs
every feature the golden host had.

- **Across families:** Intel feature sets nest, `c3 ⊂ n4 ⊂ c4`. A C3 golden restores on
  all three families, and a C4 golden restores only on C4. GKE doesn't support mixing
  families in one lineage, though (see [the advanced pattern](#advanced-widen-a-gvisor-lineage-across-intel-families)).
- **Within a family:** the `tsc_deadline_timer` flag is present on some hosts of a machine
  type and absent on others, and it follows the zone. A golden built on a host *with* the
  flag crashes on a same-family host without it. So build the golden on a host **without**
  it. GCE doesn't document which zones expose it, so check the golden host (see
  [Deploy](#deploy)).

### microVM: TSC frequency and CPUID

Two rules apply, and the second one keeps tightening:

1. **TSC frequency.** A nested hypervisor can't slow the guest clock down. A snapshot
   restores only on a host whose TSC is no faster than the host that took it. The order is
   C3 > C4 > N4: C4 → N4 restores, and N4 → C4 crashes.
2. **CPUID.** Every suspend re-stamps the snapshot with the current host's CPUID. An actor
   that has run on N4 can't go back to C3, even if its lineage started there.

Mixed families could work in one direction only, and that needs placement logic Substrate
doesn't have. **Use one machine family per microVM lineage.** Size and zone don't matter
to microVM.

## Best practices

### One ComputeClass per lineage, one family per class

Each class in [`agent-substrate-class.yaml`](./agent-substrate-class.yaml) owns one machine
family. Within it, the class falls back by size, the axis that is always safe:

| Class | Runtime | Ladder | Role |
|---|---|---|---|
| `substrate-microvm-c4` | microVM | `c4-standard-8` → `-16` → `-4` | Lineage `microvm-c4` |
| `substrate-microvm-n4` | microVM | `n4-standard-8` → `-16` → `-4` | Lineage `microvm-n4`, for new actors while C4 is short |
| `substrate-gvisor-c3` | gVisor | `c3-standard-8` → `-4` | Lineage `gvisor-c3` |
| `substrate-gvisor-mixed` | gVisor | `n4-8` → `c3-8` → `c4-8` → `n4-16` → `c4-16` | Advanced, **not supported by GKE**: widens `gvisor-c3` across Intel families |

### Add a lineage instead of a family

When you need capacity from a second family, add a **second lineage**: a second class,
WorkerPool and ActorTemplate (`substrate-microvm-n4`). Don't add a second family to the
first class. While C4 is short, create **new** actors from the N4 template. Existing C4
actors wait for C4 capacity, because they can't be moved safely. The same applies to gVisor.

For microVM, use Intel families (C4, N4, C3). C4D and C3D don't expose the
nested-virtualization `svm` flag to guests, so they have no `/dev/kvm`. N4D isn't covered by
this example. AMD families work for gVisor, each as its own lineage.

### gVisor: build the golden on a pinned pool, then widen

A gVisor golden must come from a host without `tsc_deadline_timer`, and that flag varies by
zone. So build it on a zone-pinned pool and let the rest of the lineage join afterwards:

1. A **golden pool** (`gvisor-golden`, class `substrate-gvisor-c3`) carries the lineage
   label from the start. It is pinned to a zone whose hosts lack `tsc_deadline_timer`.
2. A **serving pool** (`gvisor-c3`, same class, every zone) starts *without* the label.
3. Create the ActorTemplate. Substrate builds the golden on the only matching worker, the
   pinned one.
4. Label the serving pool into the lineage. Relabeling a WorkerPool retargets it without
   restarting its workers.

The microVM lineages don't need this: TSC and CPUID, not this flag, decide microVM restores,
and both are the same across zones of one family.

### Advanced: widen a gVisor lineage across Intel families

> **Not supported by GKE.** [About Agent Substrate on GKE](https://docs.cloud.google.com/kubernetes-engine/ai-ml/about-agent-substrate)
> says an ActorTemplate must not mix VM types. Use this only if you accept running outside
> the documented support.

Because Intel feature sets nest, a lineage whose golden was built on C3 also restores on N4
and C4. To use that, scale up `gvisor-mixed` (class `substrate-gvisor-mixed`) and label it
into `gvisor-c3` after the golden exists, as in step 4. Keep AMD and older Intel families
out of it. They don't share the C3 feature set.

### Configure the class for Substrate, not for GKE Sandbox

- **No `nodePoolConfig.sandbox`.** Substrate ships its own runsc and Cloud Hypervisor in
  the worker image (`ateom-gvisor` / `ateom-microvm`). It uses neither GKE Sandbox nor the
  `gvisor` / `microvm` RuntimeClasses.
- **Stamp `ate.dev/substrate-version` with `nodePoolConfig.nodeLabels`.** atelet's
  DaemonSet selects on it. An auto-created node without the label never runs atelet and
  never hosts a worker.
- **Set nested virtualization in `priorityDefaults` (or per rule), for microVM only.**
  `nodePoolConfig.enableNestedVirtualization` is rejected.
- **Turn `activeMigration` off.** Moving to a higher rule would move workers in the middle
  of a session, and in the mixed gVisor class it could move them across families.

### Configure the WorkerPools for the class

- **Select the class, not the machine type.** The WorkerPool's `template.nodeSelector`
  names only the compute class. Adding `node.kubernetes.io/instance-type` removes the
  class's size fallback.
- **Seed each microVM class.** microVM workers request the `ate.dev/kvm` device. atelet
  advertises it only after it is running on a node, so pending workers can't trigger a
  node pool's first node. A placeholder pod on the class can
  ([`agent-substrate-microvm-deploy.yaml`](./agent-substrate-microvm-deploy.yaml)). After
  that, workers grow the pool themselves. gVisor workers need no seed.
- **Size for one actor per worker.** A worker runs one actor at a time, so worker replicas
  equal the number of actors running at once.
- **Choose the primary family by quota.** A one-family lineage hits that family's regional
  quota sooner than a mixed ladder would.

## Files

| File | What it is |
|---|---|
| [`agent-substrate-class.yaml`](./agent-substrate-class.yaml) | The four classes above (the last one is advanced) |
| [`agent-substrate-pool.yaml`](./agent-substrate-pool.yaml) | One WorkerPool per lineage pool, with worker images pinned by digest |
| [`actor-template.yaml.tmpl`](./actor-template.yaml.tmpl) | ActorTemplate for `kubectl ate` (protojson, not a Kubernetes object), rendered once per lineage |
| [`agent-substrate-microvm-deploy.yaml`](./agent-substrate-microvm-deploy.yaml) | microVM seed Deployments, which also work as a provisioning check without Substrate |
| [`scale-demo/`](./scale-demo) | Interactive scale control tower for both runtimes |

## Deploy

Install Substrate first ([substrate-gke](https://github.com/ai-on-gke/substrate-gke)):

```bash
curl -sSL https://raw.githubusercontent.com/ai-on-gke/substrate-gke/main/install.sh | bash
```

Then:

```bash
# 0. Match the label in the class file to your install, and pick the snapshot bucket
kubectl -n ate-system get ds -o jsonpath='{..nodeSelector.ate\.dev/substrate-version}'; echo
BUCKET=<your-substrate-snapshot-bucket>

# 1. Classes, atespace, pools, seeds
kubectl apply -f agent-substrate-class.yaml
kubectl create namespace agents && kubectl ate create atespace agents
kubectl apply -f agent-substrate-pool.yaml
kubectl apply -f agent-substrate-microvm-deploy.yaml
kubectl -n agents get pods -w                      # wait for workers to be Ready

# 2. gVisor only: confirm the golden worker's host lacks tsc_deadline_timer (want 0)
NODE=$(kubectl -n agents get pods -l ate.dev/worker-pool=gvisor-golden -o jsonpath='{.items[0].spec.nodeName}')
kubectl debug node/$NODE -it --image=busybox -- grep -c tsc_deadline_timer /proc/cpuinfo

# 3. One template per lineage (the golden snapshot is built at creation)
render() { sed -e "s|\${NAME}|$1|; s|\${LINEAGE}|$1|; s|\${SANDBOX_CLASS}|$2|; s|\${SANDBOX_CONFIG}|$3|; s|\${BUCKET}|${BUCKET}|" actor-template.yaml.tmpl; }
render microvm-c4   SANDBOX_CLASS_MICROVM microvm        | kubectl ate create actor-template -f -
render gvisor-c3    SANDBOX_CLASS_GVISOR  gvisor-default | kubectl ate create actor-template -f -
kubectl ate get actor-template -a agents           # wait for GOLDEN TAG to fill in

# 4. Only now let the gVisor serving pool join the lineage
kubectl -n agents label workerpool gvisor-c3 ate.dev/lineage=gvisor-c3

# 5. Run actors
kubectl ate create actor demo-1 -a agents --template microvm-c4
kubectl ate suspend actor demo-1 -a agents
kubectl ate resume actor demo-1 -a agents
```

If step 2 prints a nonzero count, change the zone on the `gvisor-golden` pool and recreate
it before step 3. To add the N4 lineage, scale `microvm-n4` and its seed above zero, then
render and create `microvm-n4`.

## Observe

```bash
kubectl ate get workers -a agents
kubectl ate get actors -a agents                   # STATE: RUNNING / SUSPENDED / CRASHED
kubectl get nodes -L cloud.google.com/compute-class,node.kubernetes.io/instance-type,topology.kubernetes.io/zone
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.capacity.ate\.dev/kvm}{"\n"}{end}'
```

### Recognize and recover an incompatible restore

A restore onto an incompatible host leaves the actor `ACTOR_STATE_CRASHED`. The error
names the cause:

| Runtime | Error | Cause |
|---|---|---|
| gVisor | `incompatible FeatureSet: missing features: map[...]` | Host lacks a feature the golden host had (another family, or `tsc_deadline_timer`) |
| microVM | `SetTscKhz(Invalid argument ...)` | Host TSC is faster than the snapshot's |
| microVM | `Error checking CPUID compatibility` | Snapshot was last suspended on another family |

The crashed state is sticky: resume and requests are refused even after a compatible worker
appears. `kubectl ate revert actor <name> -a agents` returns the actor to `SUSPENDED` from
its last snapshot without losing state. Then resume it on a compatible worker.

## Caveats

- **No compatibility-aware scheduling.** Mixing incompatible hosts in one lineage passes
  every check up front. The failure shows up per actor, at restore time.
- **You don't pick the golden host directly.** It is whichever matching worker Substrate
  chooses at template creation. Recreating a template rebuilds the golden on whatever
  matches *then*. Before you recreate the gVisor template, remove the lineage label from the
  serving pools.
- **microVM drift is one-way and silent.** An actor whose snapshots drift to another family
  keeps working until it lands on its original family again. Revert doesn't help there,
  because the reverted snapshot carries the new CPUID.
- **Size fallback for microVM needs seed capacity.** Each size rule is a separate node pool.
  When the first size is stocked out, pending workers can't open the next size's pool,
  because no node there advertises `ate.dev/kvm` yet. Seeds fall back like any pod, so keep
  enough seed replicas (one per node) to cover the capacity you need, or use a
  CapacityBuffer with the same pod template.
- **Separate lineages fragment capacity.** Each lineage's workers serve only that lineage.
  A drained C4 pool can't borrow idle N4 workers.
- **Out of scope:** AMD families for either runtime, older Intel families (N2, C2), and
  consolidation of a worker with a *running* actor. `activeMigration` is off because of the
  last one.

## When to use which pattern

| Need | Pattern |
|---|---|
| microVM actors | One family per class, a second lineage for a second family |
| gVisor actors | One family per class, golden on a zone-pinned host without `tsc_deadline_timer`, a second lineage for a second family |
| gVisor actors that must fall back across Intel families, outside GKE support | C3 golden, then the mixed Intel class for serving |

Sandboxes that run on GKE Sandbox nodes have different constraints. See
[Agent Sandbox on ComputeClasses](../agent-sandbox).

---

## Interactive demo: GKE Agent Substrate scale control tower (`./scale-demo`)

For a control tower and dashboard that surges from 0 to 2,000 agents on either runtime, see
**[`./scale-demo/README.md`](./scale-demo/README.md)**. It covers per-family
`CapacityQuota`s and Active + Standby `CapacityBuffer`s. In Substrate mode it follows the
lineage rule above, with one WorkerPool and ActorTemplate per machine family.

![GKE Agent Substrate - scale performance control tower](./scale-demo/images/screenshot-timeseries.png)

```bash
cd scale-demo
./install.sh
./run-local.sh
# Open http://localhost:8881
```
