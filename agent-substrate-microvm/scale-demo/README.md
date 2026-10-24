# GKE Agent Substrate - scale performance control tower

An interactive real-time observability and surge-scaling control tower for **GKE Agent Substrate (`ate.dev/v1alpha1`)**, **Custom ComputeClasses (`cloud.google.com/v1`)**, **CapacityQuotas (`autoscaling.x-k8s.io/v1beta1`)**, and **Hybrid Active + Standby CapacityBuffers (`autoscaling.x-k8s.io/v1beta1`)**.

This demo visualizes how a bursty AI agent platform scales from `0` to `2,000` hardware-isolated microVM agents across a 5-tier ComputeClass priority ladder (`c3` &rarr; `n4` &rarr; `c4` &rarr; `c2` &rarr; `n2`), claiming pre-warmed active nodes and S3-suspended standby VMs while enforcing per-family vCPU ceilings and disambiguating quota caps from zonal GCE stockouts.

![Surge controls, KPI summary cards, and real-time telemetry charts](./images/screenshot-timeseries.png)

![ComputeClass observability matrix with status disambiguation and CapacityQuota progress bars](./images/screenshot-status.png)

---

## Architecture & capabilities demonstrated

1. **Dual sandbox runtimes (`microVM (Kata)` and `gVisor (runsc)`)**:
   - Workloads support both hardware-isolated microVM sandboxing (`runtimeClassName: microvm`, Kata Containers + Cloud Hypervisor via `/dev/kvm`, `priorityDefaults.enableNestedVirtualization: true`) targeting `ComputeClass/agent-substrate-microvm` and process-isolated application kernel sandboxing (`runtimeClassName: gvisor`, gVisor runsc) targeting `ComputeClass/agent-substrate-gvisor`.
   - The Control Tower provides a live radio selector to toggle between `microVM (Kata)` and `gVisor (runsc)`.
   - When switching runtimes, the backend reconciles the change: scales the inactive runtime's `Deployment` and `WorkerPool` to `0`, scales the newly active runtime to the target replica count, and updates `CapacityBuffer` `podTemplateRef` pointers so warm active and S3-suspended standby buffers cleanly hand off between runtime tiers without orphaned resources.
2. **5-tier ComputeClass priority ladder (`manifests/01-compute-class.yaml`)**:
   - **Priority 0 (`c3-standard-8`)**: Primary compute-optimized tier.
   - **Priority 1 (`n4-standard-8`)**: First general-purpose fallback tier.
   - **Priority 2 (`c4-standard-8`)**: Next-generation compute-optimized fallback tier.
   - **Priority 3 (`c2-standard-8`)**: Secondary compute-optimized fallback tier.
   - **Priority 4 (`machineFamily: n2`, `minCores: 8`, `minMemoryGb: 32`)**: Elastic catch-all floor for broad zonal availability.
   - **Fast consolidation**: Configured with `spec.autoscalingPolicy.consolidationDelayMinutes: 1` (adjustable live from the UI).
3. **Declarative per-tier & class-wide `CapacityQuota` (`manifests/02-capacity-quotas.yaml`)**:
   - Enforces pre-flight vCPU ceilings per machine family (`c3-tier-cap: 24`, `n4-tier-cap: 96`, `c4-tier-cap: 96`, `c2-tier-cap: 96`) and across the entire ComputeClass (`agent-substrate-class-cap: 720`).
   - When a priority tier reaches 100% of its `CapacityQuota`, Cluster Autoscaler skips proposing new nodes in that tier pre-flight and immediately spills over to the next priority rule without waiting for GCE failure backoffs.
4. **Hybrid Active + Multi-Family Standby `CapacityBuffer`s (`manifests/03-standby-buffer.yaml`)**:
   - **Active capacity buffer (`buffer.x-k8s.io/active-capacity`)**: Keeps pre-booted, running nodes ready for immediate pod scheduling (`<2s`).
   - **Standby capacity buffers (`buffer.gke.io/standby-capacity`)**: Pre-provisions GCE VMs across `c3`, `n4`, `c4`, `c2`, and `n2` and suspends them to ACPI S3 state (`Suspended=True` / taint `buffer.gke.io/standby-capacity-node-suspended:NoSchedule`), resuming them in ~15–20s during traffic surges without paying running compute costs while idle.
5. **Real-time telemetry & spillover disambiguation**:
   - Streams cluster state via Server-Sent Events (SSE) on port `8881`.
   - Separates active running nodes from suspended S3 standby nodes in the **ComputeClass node count** chart so pre-provisioned standby capacity never inflates active node counts.
   - Tracks both **Target agents** and **Active agents** (`readyMicrovms * actorsPerWorker`) alongside microVM pod readiness and scheduling/boot latencies.
   - Disambiguates every priority tier's scaling state into explicit badges:
     - `Active`: All zones and quotas available for scale-up.
     - `CAPPED · CAPACITYQUOTA`: Pre-flight Kubernetes `CapacityQuota` reached (`100%`) — spilling over to next tier.
     - `CONSTRAINED · STOCKOUT (X/Y configs)`: Partial zonal GCE stockout (`ProvisioningConstrained`) — healthy zones in the tier continue scaling.
     - `SUSPENDED · STOCKOUT`: Full-tier GCE stockout (`ProvisioningSuspended` with `reason: OutOfResources`).
     - `SUSPENDED · GCE QUOTA`: Full-tier GCP regional quota exhaustion (`ProvisioningSuspended` with `reason: QuotaExceeded`).
     - `PROVISIONING`: GCE VMs actively booting (`NodeProvisioningInProgress`).

---

## Prerequisites & dependencies

### 1. Google Cloud & GKE cluster requirements

To run against a live GKE cluster, your cluster must meet the following requirements:

- **GKE version & release channel**: **GKE Standard 1.37+** enrolled in the **`Rapid` release channel** (`--release-channel=rapid`).
  - *Note*: `CapacityQuota` (`autoscaling.x-k8s.io/v1beta1`) and `CapacityBuffer` standby S3 suspend/resume (`buffer.gke.io/standby-capacity`) require GKE 1.37+ on the Rapid channel. (If you do not have a 1.37+ cluster yet, you can still run the control tower in standalone `--mock` simulation mode.)
- **Node Auto-Provisioning (NAP)**: Enabled (`--enable-autoprovisioning`) with sufficient cluster-wide CPU and memory limits (at least `800` vCPUs and `3200` GB memory recommended to test the full 2,000-agent surge).
- **Autoscaling profile**: `--autoscaling-profile=optimize-utilization` is strongly recommended for rapid scale-down of idle and standby nodes.
- **Region & machine availability**: Choose a region offering Intel nested-virtualization-capable machine series (`c3`, `n4`, `c4`, `c2`, `n2`), such as `us-central1`.
- **Workload Identity & Dataplane V2**: Required if installing the upstream Agent Substrate controller (`--workload-pool=<PROJECT_ID>.svc.id.goog --enable-dataplane-v2`).

#### Create a compatible GKE 1.37+ cluster from scratch

```bash
export PROJECT_ID="your-gcp-project-id"
export CLUSTER_NAME="agent-substrate-demo"
export REGION="us-central1"

gcloud container clusters create "${CLUSTER_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --release-channel=rapid \
  --cluster-version=1.37 \
  --machine-type=n2-standard-2 \
  --num-nodes=1 \
  --enable-autoprovisioning \
  --min-cpu=0 \
  --max-cpu=1000 \
  --min-memory=0 \
  --max-memory=4000 \
  --autoscaling-profile=optimize-utilization \
  --enable-dataplane-v2 \
  --workload-pool="${PROJECT_ID}.svc.id.goog"

gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}"
```

### 2. Local workstation dependencies

- **`gcloud` CLI**: Authenticated (`gcloud auth login` and `gcloud auth application-default login`).
- **`kubectl` (`v1.28+`)**: Configured with `cluster-admin` permissions on the target cluster (includes built-in `kubectl apply -k` Kustomize support).
- **`python3` (`3.10+`) with `venv`**: Required when running the FastAPI telemetry server locally via `./run-local.sh` (automatically creates `.venv` and installs dependencies from `app/requirements.txt`: `fastapi`, `uvicorn[standard]`, `kubernetes`, `pydantic`, `pytest`, `httpx`).
- **`jq`**: Required only if running the optional CLI diagnostic scripts in `scripts/`.

---

## Step-by-step setup & installation

Navigate to the `scale-demo` directory:

```bash
cd agent-substrate-microvm/scale-demo
```

### Step 1: Install the Kubernetes manifests on your cluster

Run the installation script to apply the `agent-substrate-demo` namespace, structural Agent Substrate CRDs (`WorkerPool`, `ActorTemplate`), the `agent-substrate-microvm` `ComputeClass`, `CapacityQuota`s, `CapacityBuffer`s, and the dashboard deployment:

```bash
./install.sh
```

What `./install.sh` applies (via `kustomization.yaml`):
1. [`manifests/00-namespace-and-crds.yaml`](./manifests/00-namespace-and-crds.yaml): Creates the `agent-substrate-demo` namespace and registers structural `WorkerPool` and `ActorTemplate` CRDs (`ate.dev/v1alpha1`) if not already installed on the cluster.
2. [`manifests/01-compute-class.yaml`](./manifests/01-compute-class.yaml): Configures the 5-tier `agent-substrate-microvm` `ComputeClass` (`c3` &rarr; `n4` &rarr; `c4` &rarr; `c2` &rarr; `n2`) with `sandbox.type: microvm`, `enableNestedVirtualization: true`, and `consolidationDelayMinutes: 1`.
3. [`manifests/02-capacity-quotas.yaml`](./manifests/02-capacity-quotas.yaml): Creates `c3-tier-cap` (`24` vCPU), `n4-tier-cap` (`96` vCPU), `c4-tier-cap` (`96` vCPU), `c2-tier-cap` (`96` vCPU), and `agent-substrate-class-cap` (`720` vCPU).
4. [`manifests/03-standby-buffer.yaml`](./manifests/03-standby-buffer.yaml): Creates `PodTemplate`s and 6 `CapacityBuffer`s (`agent-active-buffer` + 5 per-family `agent-standby-buffer-*` resources) in `agent-substrate-demo`.
5. [`manifests/04-agent-substrate-pool.yaml`](./manifests/04-agent-substrate-pool.yaml): Deploys the `WorkerPool`, `ActorTemplate`, and `agent-substrate-microvm-workers` Deployment (initially scaled to `0` replicas) in `agent-substrate-demo`.
6. [`manifests/05-dashboard.yaml`](./manifests/05-dashboard.yaml): Deploys the in-cluster `ccc-agents-dashboard` Service and Deployment on port `8881` in `agent-substrate-demo`.

*(Optional)* If you also want to install the full upstream Agent Substrate controller on your cluster:
```bash
curl -sSL https://raw.githubusercontent.com/ai-on-gke/substrate-gke/main/install.sh | bash
```

#### Waiting for standby `CapacityBuffer` nodes to provision and suspend

Standby `CapacityBuffer`s (`buffer.gke.io/standby-capacity`) take a few minutes after initial installation to provision new GCE VMs, complete node initialization (`buffer.gke.io/standby-capacity-init-time: "2m"`), and transition into a suspended ACPI S3 state.

Watching `kubectl get nodes` is a convenient way to verify that the standby nodes have first provisioned (`Ready`), been cordoned (`Ready,SchedulingDisabled`), and finally settled into the suspended state (**`NotReady,SchedulingDisabled`**):

```bash
watch kubectl get nodes
```

![kubectl get nodes showing active Ready nodes alongside suspended NotReady,SchedulingDisabled standby buffer nodes](./images/get-nodes-suspended.png)

Once the standby nodes display `NotReady,SchedulingDisabled` (with condition `Suspended=True`), your multi-family standby pool is armed and ready for rapid S3 resume on surge.

---

### Step 2: Launch & access the control tower UI (Port `8881`)

You can run the control tower in three ways:

#### Option A: Run locally against your active `kubectl` context (Recommended)

Running the FastAPI server locally uses your local `kubeconfig` credentials and provides sub-20ms UI response times:

```bash
./run-local.sh
# To start directly with gVisor sandbox runtime:
# ./run-local.sh --runtime gvisor
```

Then open **`http://localhost:8881`** in your browser.
*(To override the bind address or port: `HOST=0.0.0.0 PORT=8881 ./run-local.sh`)*

#### Option B: Port-forward to the in-cluster dashboard Service

If you prefer not to run Python locally, forward port `8881` to the `ccc-agents-dashboard` Service deployed by `./install.sh`:

```bash
kubectl port-forward svc/ccc-agents-dashboard 8881:8881 -n agent-substrate-demo
```

Then open **`http://localhost:8881`** in your browser.

#### Option C: Standalone mock simulation mode (No GKE cluster required)

To explore the UI, priority spillover mechanics, and quota controls offline without a Kubernetes cluster:

```bash
./run-local.sh --mock
# Or simulate gVisor runtime offline:
# ./run-local.sh --mock --runtime gvisor
```

Then open **`http://localhost:8881`** in your browser.

---

### Accessing the UI from a local laptop when running on a remote VM / bastion host

If you are running `./run-local.sh` or `kubectl port-forward` on a remote Linux workstation, Cloud VM, or bastion host and want to view the browser UI on your local laptop, forward local port `8881` over SSH from your laptop terminal:

```bash
# Standard SSH local port forwarding from your laptop:
ssh -N -L 8881:127.0.0.1:8881 <your-username>@<remote-host>

# Or if connecting to a Google Cloud Compute Engine VM via gcloud:
gcloud compute ssh <vm-name> --zone=<zone> -- -NL 8881:127.0.0.1:8881
```

Keep the SSH terminal open and navigate to **`http://localhost:8881`** in your laptop browser.

---

## Using the control tower

### 1. Surge controls, KPI cards & time-series telemetry charts

![Control tower surge controls, KPI cards, and 5-minute time-series telemetry charts](./images/screenshot-timeseries.png)

- **Sandbox runtime (`microVM (Kata)` / `gVisor (runsc)`)**: Live radio selector switching the active agent workload and capacity buffer pod templates between Kata microVMs (`runtimeClassName: microvm`) and gVisor application kernels (`runtimeClassName: gvisor`).
- **Active agents target (`0` – `2,000`)**: Drag the slider or click one of the **Quick presets**, then click **Apply surge** (presets apply automatically):
  - **`0: Idle (Buffers warm)`**: Scales active sandbox worker pods to `0` while keeping active and standby `CapacityBuffer`s warm.
  - **`100: Active + standby buffer claim`**: Launches `50` sandbox worker pods (at `2:1` density), immediately claiming warm active buffer nodes and triggering S3 standby VM resume.
  - **`600: Tier 0 → 1 (c3 → n4 spillover)`**: Launches `300` sandbox worker pods (`~12` nodes). Tier 0 (`c3`) hits its `24` vCPU (`3` node) `CapacityQuota` (`CAPPED · CAPACITYQUOTA`) and spills over into Tier 1 (`n4`).
  - **`1,200: Tier 1 → 2/3 (n4/c4/c2 spillover)`**: Launches `600` sandbox worker pods (`~24` nodes), cascading across `c3`, `n4`, and `c4`/`c2`.
  - **`2,000: Full 5-family cascade (to n2 floor)`**: Launches `1,000` sandbox worker pods (`~40` nodes), saturating `c3`, `n4`, `c4`, and `c2` quotas and spilling into the elastic `n2` catch-all floor.
- **Actor:Sandbox density (`1:1`, `2:1`, `4:1`)**: Controls how many logical AI agents pack into each sandbox worker pod (`targetSandboxes = ceil(targetAgents / actorsPerWorker)`).
- **Active buffer nodes (`0` – `50`)**: Adjusts `spec.replicas` on `CapacityBuffer/agent-active-buffer`.
- **Standby buffer nodes (`0` – `100`)**: Distributes standby replicas across all priority families (`agent-standby-buffer-<family>`).
- **Consolidation delay (`1` – `1440` min)**: Live-patches `ComputeClass.spec.autoscalingPolicy.consolidationDelayMinutes`.
- **Reset data & metrics**: Clears the rolling chart history, resets pod scheduling/boot latency averages & P95 window to the current timestamp, and re-baselines per-tier `Provisioned / Consolidated / Migrated` counters to `0`.
- **Graph time scale (`30s`, `1m`, `5m`, `15m`, `30m`, `60m`)**: Zooms all three charts across up to 60 minutes of buffered telemetry (automatically downsampled on `5m+` windows to prevent visual congestion):
  1. **Active node count over time by machineFamily**: Stacked area chart of **active running nodes** per family (excluding suspended S3 standby nodes so idle standby capacity never looks like over-provisioning).
  2. **Active agents & sandbox pod count over time**: Plots `Target agents` (dashed sky-blue), `Active agents` (solid sky-blue), `Target sandboxes` (dashed white), `Ready sandboxes` (green), and `Pending sandboxes` (amber).
  3. **Average pod scheduling & sandbox creation latency over time**: Tracks average pod scheduling latency (`PodScheduled`) and sandbox creation latency (`ContainersReady - PodScheduled`).

### 2. ComputeClass observability matrix & status disambiguation

![ComputeClass observability matrix showing per-tier status badges, standby S3 counts, and CapacityQuota usage bars](./images/screenshot-status.png)

- **Status condition & backoff timer**: Click or hover the `?` header icon or any row's `i` badge to inspect the live GKE `status.priorityStatuses[]` condition, excluded zonal configuration ratio (e.g., `(3/6 configs)`), and active backoff countdown timer.
- **Nodes (family / annotated / standby)**: Displays active running nodes in that family alongside the `ccc_priority_index` annotation count and suspended S3 standby buffer count (`standby=X/Y S3`).
- **Quota / cap limit & usage**: Unified progress bars showing real-time vCPU consumption against each tier's `CapacityQuota` (`c3-tier-cap`, `n4-tier-cap`, `c4-tier-cap`, `c2-tier-cap`) or the class-wide `agent-substrate-class-cap`.
- **Live `CapacityQuota` API adjustments**: Dynamically raise or lower any tier's `CapacityQuota` vCPU ceiling at runtime via `POST /api/quotas`:
  ```bash
  curl -X POST http://localhost:8881/api/quotas \
    -H "Content-Type: application/json" \
    -d '{"quotaName": "n4-tier-cap", "cpuLimit": 128}'
  ```

---

## Running unit & integration tests

```bash
python3 -m venv .venv
.venv/bin/pip install -q -r app/requirements.txt
.venv/bin/pytest -q
```

---

## Stopping the demo & cleanup

### Spin down all ComputeClass nodes while keeping the cluster
To scale all workloads and active/standby buffers to `0` so Cluster Autoscaler consolidates all `agent-substrate-microvm` nodes:

```bash
kubectl scale deployment/agent-substrate-microvm-workers --replicas=0 -n agent-substrate-demo
for buf in $(kubectl get capacitybuffers -n agent-substrate-demo -o name); do
  kubectl patch "${buf}" -n agent-substrate-demo --type=merge -p '{"spec":{"replicas":0}}'
done
```

### Full uninstall from the cluster
To remove all demo workloads, `CapacityBuffer`s, `CapacityQuota`s, the `agent-substrate-microvm` `ComputeClass`, and the `agent-substrate-demo` namespace:

```bash
./uninstall.sh
```
