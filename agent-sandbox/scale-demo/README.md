# GKE Agent Sandbox - scale performance control tower

A live control tower for [Agent Sandbox](../README.md) agents on **custom ComputeClasses**,
**CapacityQuotas** and **CapacityBuffers**. It is the Agent Sandbox counterpart of the
[Agent Substrate control tower](../../agent-substrate/scale-demo): same UI and charts, but
the agents are Agent Sandbox objects (`agents.x-k8s.io`) and not Substrate actors.

| Mode | One agent is | Warm layer | Ladder | What it shows |
|---|---|---|---|---|
| **Stateless** (default) | A `SandboxClaim`, gVisor or microVM | `SandboxWarmPool` plus active and standby `CapacityBuffer`s | Wide, 5 tiers: `c3` → `n4` → `c4` → `c2` → `n2` | Surging from 0 to 2,000 agents across families, under per-family quotas |
| **Snapshots** (whole-pod) | A gVisor session `Sandbox` that can be suspended to a Pod snapshot and resumed | Active and standby `CapacityBuffer`s | One family, size fallback: `n4-standard-8` → `16` → `4` | Suspend and resume of stateful sessions, and why a whole-pod snapshot class keeps to one family |
| **Snapshots, rootfs-only** | The same session `Sandbox`, with a rootfs-only Pod snapshot | Active and standby `CapacityBuffer`s | Wide, 5 tiers like stateless gVisor | Files survive a resume, memory and processes do not, and sessions may land on any family |

Pick a mode with `./install.sh --mode=stateless|snapshots|snapshots-rootfs` (and
`DEMO_MODE` for `run-local.sh`). What each option preserves:

| Option | What is preserved | What is lost | Machine-family fallback | Typical resume cost | When to use |
|---|---|---|---|---|---|
| **Stateless** (no snapshots) | Nothing. A claimed sandbox keeps its node until the session ends | Everything on any pod restart; eviction is a silent reset | Any family and size: wide ladder | No resume. A new sandbox starts cold, or a claim adopts a warm-pool sandbox | Disposable sessions, or state held outside the pod |
| **Pod Snapshots, rootfs-only** (`snapshotScope: rootfs-only`) | Files on the container root filesystem | Memory, running processes (the container starts again) and `/tmp` (tmpfs under gVisor) | Crosses families: wide ladder | Restore plus a process restart. Not quoted here: measure it with Resume → Ready in the demo | State lives in files (workspaces, checkouts, notebooks) and the process can be restarted |
| **Pod Snapshots, whole-pod** (`snapshotScope: whole-pod`) | Memory, processes, `/tmp` and the root filesystem | Nothing, if the restore applies. On another machine family (or different CPU features) the pod cold-starts silently | **One family**, size fallback only, with Guaranteed QoS | Restore. Not quoted here: measure it with Resume → Ready in the demo | State lives in memory or running processes and cannot be rebuilt |

The surge controls, KPI cards, node charts and the ComputeClass status matrix (Active,
`CAPPED · CAPACITYQUOTA`, `CONSTRAINED · STOCKOUT`, `SUSPENDED · GCE QUOTA`,
`PROVISIONING`) behave as described in the
[Substrate tower's README](../../agent-substrate/scale-demo/README.md#using-the-control-tower).

---

## How it works

### Stateless mode

- **One agent = one `SandboxClaim`** named `agent-<runtime>-NNNN` against
  `agent-<runtime>-template`. The tower creates and deletes claims to match the target.
  An agent counts as active when its claimed sandbox pod is Ready.
- **`SandboxWarmPool`** (`agent-<runtime>-warmpool`) keeps pre-booted sandboxes. A claim
  takes one if any is ready (a warm hit) and otherwise creates a sandbox cold. The
  **Warm pool** input sets its replicas live (0–200).
- **Two runtimes**, each with its own class: `agent-sandbox-demo-microvm` (nested
  virtualization, Intel families) and `agent-sandbox-demo-gvisor`. The runtime selector
  scales the other runtime's claims and warm pool to 0 and repoints the buffers.
- **CapacityQuotas** cap `c3` at 24 vCPU and `n4`, `c4`, `c2` at 96 each, plus a class
  cap of 720. A full tier spills to the next before GCE is even asked.
- **CapacityBuffers:** one active buffer (2 running placeholder pods) and one standby
  buffer per family (suspended VMs that resume in seconds).

### Snapshots mode

- **One agent = one session `Sandbox`** named `session-NNNN`, built from
  `agent-session-template` and carrying the pod label `session=<name>`. The
  `PodSnapshotPolicy` groups snapshots by that label, so each session gets its own
  snapshot lineage.
- **Session control** (one state-aware button plus a `Sessions: ...` pill): reads "Snapshot & suspend all" while sessions run, "Resume all" once they are suspended (with a secondary link when mixed), and is disabled with a spinner while sessions are snapshotting or starting. It calls `POST /api/sessions` with `suspend` or `resume`.
- **Suspend** (every running session): for each one the tower creates a
  `PodSnapshotManualTrigger`, waits until it reports `status.snapshotCreated`, then sets
  the Sandbox's `spec.operatingMode: Suspended`. The pod is deleted and its node can
  scale down.
- **Resume** (every suspended session) sets `operatingMode: Running`. The new pod restores from the latest snapshot.
  The KPI row shows snapshot and resume latency.
- **One family, three sizes.** A whole-pod snapshot restores only on the machine family
  that took it, so the class is `n4-standard-8` → `n4-standard-16` → `n4-standard-4`.
  Sessions use Guaranteed QoS (requests equal limits), which lets a snapshot taken on one
  size restore on another.
- **One class-wide CapacityQuota** (`sbx-demo-snapshots-cap`, 192 vCPU). It has to be
  class-wide: see [Sharp edges](#sharp-edges).

### Snapshots, rootfs-only mode

- **Same session flow as snapshots mode**: one `Sandbox` per agent, `session` label, snapshot
  then suspend, resume sets `operatingMode: Running`. The policy sets
  `snapshotScope: rootfs-only`.
- **Wide ladder, no class of its own.** A rootfs-only snapshot restores across machine
  families, so sessions run on `agent-sandbox-demo-gvisor` with its quotas and buffers from
  `manifests/01-03`. Nodes are grouped by family as in stateless mode.
- **The session program shows what survives.** Each session keeps a counter in a file under
  `/var/tmp` (root filesystem, not `/tmp`) and another in memory, and logs both as one JSON
  line. The tower reads the last line before the snapshot and again once the resumed pod is
  Ready. **Files kept** counts sessions whose state file is the same one and did not go back
  in time; **Memory kept** counts sessions still running the same process. Sessions whose
  log could not be read are left unchecked, not counted. This needs `pods/log` get on the
  dashboard's ClusterRole (in `manifests/05-dashboard.yaml`).
- **Expected result**: Files kept reads N of N (N = sessions checked), Memory kept 0 of N. Both show a dash until the first resume has been read.

---

## Prerequisites

Versions and setup steps change, so treat the linked GKE pages as the source of truth.

| Dependency | Mode | Minimum GKE | Docs |
|---|---|---|---|
| Agent Sandbox add-on (`--enable-agent-sandbox`) | Both | 1.36.3-gke.1767000 | [Enable Agent Sandbox on GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/how-install-agent-sandbox) |
| GKE Sandbox, gVisor | Both | — | Enabled per node by the class's `nodePoolConfig.sandbox`. [Isolate workloads by using GKE Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods#choose-sandbox-auto-created) |
| GKE Sandbox, microVM, with nested virtualization | Stateless, microVM runtime | 1.37.0-gke.4713000 | [GKE Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods#choose-sandbox-auto-created), [Use nested VMs with GKE clusters](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nested-virtualization) |
| Pod Snapshots (`--enable-pod-snapshots`), a Cloud Storage bucket, IAM, Workload Identity | Snapshots | 1.35.3-gke.1234000 | [Save and restore Agent Sandbox environments with Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/agent-sandbox-pod-snapshots), restore rules in [About GKE Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots#limitations) |
| `CapacityBuffer`, active | Both | 1.35.2-gke.1842000 | [About capacity buffers](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/capacity-buffer), [Configure capacity buffers](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-capacity-buffer) |
| `CapacityBuffer`, standby (suspended nodes) | Both | 1.36.0-gke.2253000 | Same pages |
| `CapacityQuota` | Both | 1.36.2-gke.2064000 | [Configure granular resource limits](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-capacity-quotas) |
| Node pool auto-creation by ComputeClasses | Both | 1.33.3-gke.1136000 | [About custom ComputeClasses](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-custom-compute-classes) |

So stateless mode with microVM needs **1.37.0-gke.4713000+**; everything else runs on
**1.36.3-gke.1767000+**. `install.sh` checks for the CRDs and the `microvm` RuntimeClass
and stops with the enable command if one is missing.

Workstation: `gcloud`, `kubectl` with cluster-admin, and `python3` 3.10+ with `venv` for
running the tower locally. No GKE cluster at all? Use [mock mode](#run-the-tower).

---

## Get it

```bash
cd agent-sandbox/scale-demo
```

### Install: stateless mode

```bash
./install.sh                      # same as --mode=stateless
```

This applies `manifests/` (namespace `agent-sandbox-demo`, both classes, quotas, buffers,
templates, empty warm pools, the dashboard) with `kubectl apply -k .`.

### Install: snapshots mode

```bash
export SNAPSHOT_BUCKET=my-agent-snapshots   # lowercase; must be globally unique
./scripts/setup-snapshot-bucket.sh "$SNAPSHOT_BUCKET" us-central1
./install.sh --mode=snapshots --bucket="$SNAPSHOT_BUCKET"
```

`setup-snapshot-bucket.sh` creates the bucket the way Pod Snapshots requires it
(hierarchical namespace, soft delete off, uniform bucket-level access), creates the
`sandboxes/` managed folder, and grants the session KSA and the GKE service agent access.
If the bucket exists, it checks those settings and only adds what is missing.
`install.sh` then applies `manifests/00`, `snapshots/01–04` and the dashboard.

For rootfs-only snapshots, run `./install.sh --mode=snapshots-rootfs --bucket="$SNAPSHOT_BUCKET"`
with the same bucket. It applies `manifests/00–03` (the wide gVisor class, quotas and
buffers), `snapshots-rootfs/04-sessions.yaml` and the dashboard.

`install.sh` returns once the dashboard answers (readiness probe), so you can port-forward
right away. The standby buffers then create nodes and suspend them; the header's
**Capacity** badge (amber and pulsing) reads "warming up" until every buffer is ready and the standby nodes are
suspended, then "ready to scale". You can scale earlier, but the run then measures cold node
starts instead of the warm layer.

The badge goes amber again after a burst or scale-down: the standby nodes that served the burst
are consumed, and replacements take about 3–4 minutes (boot, the buffer's 2-minute
`standby-capacity-init-time`, then suspend). Wait for green again before the next measured run.

### Run the tower

```bash
./run-local.sh                        # against your current kubectl context, port 8881
DEMO_MODE=snapshots ./run-local.sh    # after a snapshots-mode install
./run-local.sh --runtime gvisor       # start stateless mode on gVisor
./run-local.sh --mock                 # no cluster: simulated nodes, quotas and stockouts
DEMO_MODE=snapshots ./run-local.sh --mock
DEMO_MODE=snapshots-rootfs ./run-local.sh    # after a snapshots-rootfs install
DEMO_MODE=snapshots-rootfs ./run-local.sh --mock   # Files/Memory kept are simulated
```

Or use the in-cluster dashboard:
`kubectl -n agent-sandbox-demo port-forward svc/sbx-demo-dashboard 8881:8881`.

Run one tower at a time. A live `run-local.sh` scales the in-cluster dashboard to 0 on
start, so the two don't fight over the claims; scale it back to 1 to use it again.

Then open <http://localhost:8881>. Presets: stateless 0 / 10 / 150 / 300 / 500 agents
(slider to 2,000); snapshots 0 / 20 / 100 / 250 sessions (slider to 400); snapshots-rootfs 0 / 20 / 100 / 300
sessions (slider to 400).

### Guided tour

The page opens with an optional **Guided tour** panel (bottom right) that walks a first-time
user through the mode they are in: wait for the Capacity badge (its first step has a "Show me" link that highlights it), a small preset, a large preset
(stateless also covers the runtime toggle; snapshots cover suspend and resume), then Reset and
cleanup. Steps tick off from the live telemetry where there is a signal and otherwise have a
"Mark done" button. Collapse it with the arrow, close it with the X, and reopen it with the
**Guide** button in the header; the choice is remembered in your browser.

### Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

### Uninstall

```bash
./uninstall.sh
```

This removes the claims, warm pools, sandboxes, snapshot triggers and policies, buffers,
quotas, both modes' classes, the dashboard RBAC and the namespace. It leaves the
snapshot bucket; delete it with `gcloud storage rm -r gs://$SNAPSHOT_BUCKET`.

---

## Sharp edges

- **Live snapshots mode shows size fallback only on a real stockout.** CapacityQuota
  rejects instance-type selectors, so there is no way to cap `n4-standard-8` alone and
  force the spill to `n4-standard-16`. The class-wide 192 vCPU cap is the only quota.
  Mock mode simulates an `n4-standard-8` stockout at 6 nodes so you can see the fallback.
- **Suspend needs a finished snapshot first.** Suspending a Sandbox without one loses the
  session. The tower waits for `status.snapshotCreated` on the trigger and skips the
  suspend if the snapshot doesn't arrive in time.
- **Pod Snapshots needs its node agent.** Each node runs a `pod-snapshot-agent` pod
  (DaemonSet in `gke-managed-pod-snapshots`). Without it a `PodSnapshotManualTrigger`
  stays at `observedGeneration: 1` forever, with no events and no error. If triggers
  never complete, check `kubectl get ds -n gke-managed-pod-snapshots`. If a `--disable-pod-snapshots` left that namespace stuck `Terminating`, re-enabling won't recover it: the agent pods keep a `podsnapshot.gke.io/token-finalizer` that only the removed controller clears. Remove that finalizer from the leftover pods; the add-on then recreates the namespace and DaemonSet within a minute.
- **Turning Pod Snapshots on or off pauses node auto-creation.** `--enable-pod-snapshots`
  and `--disable-pod-snapshots` are cluster updates that hold the cluster in
  `RECONCILING`; in our runs, about 10 minutes each. Existing nodes keep running, but
  no ComputeClass can create nodes: every priority reports `NodeProvisioning ... failed
  due to the InternalError error`, and pending agents wait. Toggle the feature before a
  demo, not during one.
- **Snapshots restore on the same family and CPU features only.** That is why the
  snapshots class has one family. Don't add a second family to it to gain capacity:
  sessions would resume onto nodes that can't restore them.
- **rootfs-only keeps files, not memory.** After a resume the process starts again and
  `/tmp` is empty. Keep state in files on the root filesystem. In mock mode the Files/Memory
  kept numbers are simulated; only a live run reads them from the pods.
- **The rootfs session program is a demo.** It assumes `python3` in the session image and a
  writable `/var/tmp` for uid 1000. If the state file cannot be written, Files kept reads 0
  after a resume even though the restore worked.
- **Budget.** At the top presets the stateless ladder can reach the 720 vCPU class cap.
  Lower `sbx-demo-class-cap` in `manifests/02-capacity-quotas.yaml` if your project's
  CPU quota or budget is smaller. Our own validation runs stayed under 400 vCPU.
- **A cold claim is slower than the warm pool suggests.** With the warm pool at 0, every
  claim creates its sandbox and may also wait for a node. The claim→Ready KPI separates
  warm hits from cold starts.
- **Standby buffers don't support GPUs, local SSDs or nodes over 208 GB of memory.** The
  shipped ladders avoid all three.
