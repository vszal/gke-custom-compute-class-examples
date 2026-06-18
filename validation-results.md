# GKE ComputeClass Examples Validation Results

This report compiles the validation results, configurations, and staged fixes for all GKE ComputeClass example manifests across the workspace.

### Validation Metadata
- **Validation Date:** June 11, 2026
- **Git Commit (Shorthash):** `94816f2`
- **Validation Method:** This run re-validated **stateful-db** only (dynamic-rwo doc/comment additions; manifests structurally unchanged) via **client-side** dry-run (`kubectl apply --dry-run=client`) + YAML schema lint — server-side dry-run was not run this session (shared-cluster mutation not authorized). Rows 1–7 and 9–19 carry forward their **PASS** from the prior server dry-run at `f77dd08` (June 10, 2026).

### Addendum — `restrict-usage` (new)
- **Date:** June 18, 2026 · **Commit:** `2c5b58b` (working tree, restrict-usage untracked) · **Context:** `gke_vsz-demo_us-central1_ccc-accel` (GKE 1.35.5-gke.1163000)
- **Method:** Full live routine — applied class + `restricted-demo` ns + RBAC + VAP, ran **server-side** dry-runs, then cleaned up (cluster verified pristine). RBAC/VAP files sit outside the standard class/workload globs and were applied/removed manually per the folder README.
- **Result:** **PASS** ✅ — compliant workload admitted; all three consumption paths (nodeSelector, nodeAffinity, wildcard toleration) **denied**; `apps/v1` Deployment controller **denied** (confirms `spec.template.spec` extraction + multi-kind `matchConstraints`); same request in an out-of-scope namespace **admitted** (binding `namespaceSelector` scoping); RBAC `auth can-i` → bound editor group `yes` on create/update/delete, non-member `no`.
- **Note:** `rbac-editor.yaml` ships the literal `<GROUP_DOMAIN>` placeholder — verification impersonated the as-applied group name; users replace it before deploy.

> **Note on GKE Validating Webhooks:** For validating admission controllers (such as GKE Warden constraints) to resolve ComputeClass features and selectors correctly during dry-run checks of workload manifests, the target `ComputeClass` resource must be deployed to the cluster. When the classes are deployed, GKE Warden resolves the configurations dynamically and validates the workloads with no redundant selectors required.

---

## Validation Summary Table

| # | Folder | Status | Files Checked | Issues / Staged Fixes | Notes & Best Practices |
|---|---|---|---|---|---|
| 1 | **machineType** | **PASS** ✅ | `type-class.yaml`<br>`type-deploy.yaml` | None | Spot-first choice fallback to On-Demand of the exact same shape works correctly. |
| 2 | **balanced-reservations** | **PASS** ✅ | `balanced-class.yaml`<br>`balanced-deploy.yaml` | None (Requires placeholder edits in production) | Correctly separates the infrastructure layer (`locationPolicy: BALANCED` on ComputeClass) and workload layer (`topologySpreadConstraints` with `DoNotSchedule` on Pod). Avoids GKE schema errors by not combining `location.zones` and `reservations.affinity: Specific`. |
| 3 | **gpu-accelerator** | **PASS** ✅ | `accelerator-class.yaml`<br>`cuda-deploy.yaml`<br>`gemma-deploy.yaml` | None | Workloads specify the required `nvidia.com/gpu:NoSchedule` toleration, bypassing the default GPU taints. |
| 4 | **kueue-ccc** | **PASS** ⚠️ | `kueue-class.yaml`<br>`ccc-queue-job.yaml`<br>`cluster-queue.yaml`<br>`local-queue.yaml`<br>`resource-flavor.yaml` | 1. **Staged Fix:** Added `nvidia.com/gpu:NoSchedule` toleration to `ccc-queue-job.yaml` so the GPU Job can schedule on GKE's auto-tainted GPU nodes.<br>2. **External Dependency:** Cluster-wide Kueue CRDs must be pre-installed to dry-run Kueue resources. | Adheres to Kueue standard patterns. ResourceFlavor nodeLabels correctly route admitted Kueue jobs to the ComputeClass. |
| 5 | **large-shape-fallback** | **PASS** ✅ | `large-shape-class.yaml`<br>`large-shape-deploy.yaml` | None | Lists large preferred shapes (`c4-standard-48`) and progressively smaller shapes (`c4-standard-16`, `c4-standard-8`) as fallbacks to protect horizontally-scalable workloads from out-of-resources stockouts. |
| 6 | **machineFamily** | **PASS** ✅ | `family-class.yaml`<br>`family-deploy.yaml` | None | Correctly ranks families (`c4` -> `c3d`) and enables active migration for automatic scaling optimization. |
| 7 | **priority-tiebreak** | **PASS** ✅ | `tiebreak-class.yaml`<br>`tiebreak-deploy.yaml` | None | Adheres strictly to `priorityScore` rules (max 3 priorities per score, all-or-nothing, unquoted integer values) to enable cost-based tie-breaking. |
| 8 | **stateful-db** | **PASS** ✅ (client-side) | `postgres-class.yaml`<br>`postgres-statefulset.yaml` | None | Pins priorities to `us-central1-a` to preserve PersistentVolume affinity. Consistently uses `hyperdisk-balanced` (Gen-4) to prevent volume attach errors from mixed disk generations. Correctly applies node-level sysctls. **New:** documents the built-in `dynamic-rwo` StorageClass (GKE 1.35.3-gke.1290000+, `type: dynamic` + `use-allowed-disk-topology: "true"`) as the supported path to broaden `priorities[]` across disk generations while keeping the autoscaler disk-topology-aware. |
| 9 | **static-node-pools** | **PASS** ✅ | `static-pools-class.yaml`<br>`static-pools-deploy.yaml` | None | References manually managed node pools instead of auto-creation. Demonstrates fallback and active migration across existing pools. |
| 10 | **storage** | **PASS** ✅ | `lssd-class.yaml`<br>`lssd-deploy.yaml` | None | Accurately configures boot disk sizes and raw local SSD count using unquoted integers. |
| 11 | **system-pool** | **PASS** ✅ | `system-pool-class.yaml` | None | Uses `whenUnsatisfiable: ScaleUpAnyway` and the namespace label to route non-DaemonSet system pods onto cheaper `n4` nodes, avoiding unmovable node scale-down blocks. |
| 12 | **arm-axion** | **PASS** ✅ | `arm-class.yaml`<br>`arm-deploy.yaml` | None | Deployment includes required `kubernetes.io/arch: arm64` toleration. Workload image is correctly configured as multi-arch (`nginx:1.27`). |
| 13 | **flexstart-batch** | **PASS** ✅ | `flexstart-class.yaml`<br>`flexstart-job.yaml` | None | Demonstrates Dynamic Workload Scheduler flex-start queuing for batch/training workloads. Job template successfully specifies the `nvidia.com/gpu` toleration. |
| 14 | **gpu-sharing** | **PASS** ✅ | `gpu-sharing-class.yaml`<br>`gpu-sharing-deploy.yaml` | None | Implements GPU time-sharing with `maxSharedClientsPerGPU: 4`. Workload replicas successfully request `nvidia.com/gpu: 1` as their shared unit. |
| 15 | **memory-optimized** | **PASS** ✅ | `mem-class.yaml`<br>`mem-deploy.yaml` | None | Correctly targets memory-optimized `m4` and `m3` families with high memory-to-vCPU requests (128 GiB memory for 2 vCPUs). |
| 16 | **spot-batch** | **PASS** ✅ | `spot-batch-class.yaml`<br>`spot-batch-deploy.yaml` | None | Spot-primary class for fault-tolerant workers. Combines `terminationGracePeriodSeconds: 25` and a `PodDisruptionBudget`. |
| 17 | **tpu-serving** | **PASS** ✅ | `tpu-class.yaml`<br>`tpu-deploy.yaml` | None | Verified with clean node selectors. When the `tpu-v6e` ComputeClass is deployed first, the GKE Warden validating admission webhook is fully satisfied as GKE is able to resolve the class dynamically. |
| 18 | **default-class-namespace** | **PASS** ✅ | `default-class.yaml`<br>`team-deploy.yaml` | None | Demonstrates setting a default ComputeClass at the namespace level via label. Plain workload manifests inherit the class automatically, simplifying team deployment configurations. |
| 19 | **hybrid-pools** | **PASS** ✅ | `hybrid-pools-class.yaml`<br>`hybrid-pools-deploy.yaml` | None | Curated manual node pools are specified on top using intent-based (rather than name-wired) priority rules, falling back underneath to auto-provisioned capacity for obtainability. |
| 20 | **restrict-usage** | **PASS** ✅ (live server dry-run) | `restricted-class.yaml`<br>`rbac-editor.yaml`<br>`restrict-usage-vap.yaml`<br>`allowed-deploy.yaml` | None | Two-layer governance. VAP denies all three consumption paths (nodeSelector / nodeAffinity / wildcard toleration) across Pods and `apps/v1` controllers; namespace scoping via binding `namespaceSelector` confirmed (out-of-scope ns admitted). RBAC `ClusterRole` (cluster-scoped CRD) restricts create/update/patch/delete to the bound group. Placeholder `<GROUP_DOMAIN>` must be set before deploy. |
