# Priority-rule fulfillment: how often did rule 0 actually win?

A ComputeClass states a preference. `kubectl get computeclass` tells you what is true right now. Neither answers the question you actually have at review time: **over the last week, how much of the capacity this class delivered came from your first-choice rule, and how much came from the fallbacks?**

This example turns the `ccc_priority_index` node annotation into Prometheus time series and ships five importable Cloud Monitoring dashboards built on them.

![Fleet fulfillment dashboard: rule-0 share, vCPU-hours on fallback rules, and per-class and per-cluster breakdowns](./dashboard-fleet.png)

## Quick start

```bash
kubectl apply -f exporter.yaml
kubectl -n ccc-observability rollout status deploy/ccc-priority-exporter
./install-dashboards.sh
```

`install-dashboards.sh` resolves the project (argument, `$PROJECT_ID`, or the active gcloud config), checks that the metrics the dashboards read are actually arriving, creates whichever dashboards are missing, and prints a bookmark-ready URL for each.

```bash
./install-dashboards.sh my-project       # an explicit project
./install-dashboards.sh --update         # replace dashboards already installed
./install-dashboards.sh --validate-only  # parse and validate, create nothing
./install-dashboards.sh --all            # include accelerator variants with no data yet
```

Re-running is safe: a dashboard already present (matched by display name) is left alone unless you pass `--update`.

## The five dashboards

| File | Scope | Answers |
|---|---|---|
| [`dashboard.json`](./dashboard.json) | one ComputeClass | *Is this class getting the shape I asked for?* The debugging view, opened when a team says their workload is on the wrong machines. Nine tiles, with separate vCPU, GPU-chip and TPU-chip charts. |
| [`dashboard-fleet.json`](./dashboard-fleet.json) | every class, every cluster, by **vCPU** | *How much capacity is landing on preferred rules, and which classes are dragging?* The FinOps view: per-class cost attribution and rule-0 share, broken out by class and by cluster. |
| [`dashboard-fleet-gpu.json`](./dashboard-fleet-gpu.json) | the same, by **GPU chip** | The same questions where the chip count, not the core count, is the unit that matters. |
| [`dashboard-fleet-tpu.json`](./dashboard-fleet-tpu.json) | the same, by **TPU chip** | As above, for TPUs. |
| [`dashboard-health.json`](./dashboard-health.json) | one cluster, right now | *Is provisioning working, and can I trust what I'm reading?* Unschedulable pods, provisions per rule in 5-minute buckets, fallback pressure, and a text tile stating its own blind spots. |

Both fleet dashboards carry `computeclass` and `cluster` template variables defaulting to `.*`, so they open fleet-wide and narrow on click.

The three fleet dashboards ask identical questions of three different units, so only the vCPU one is written by hand. [`make-fleet-variants.py`](./make-fleet-variants.py) generates the other two from it — run it after editing `dashboard-fleet.json`, or with `--check` in CI to catch drift. Every query on every dashboard was executed against live Managed Prometheus before it shipped, so no tile rests on a metric that merely appears in documentation.

[`priority-fulfillment-class.yaml`](./priority-fulfillment-class.yaml) and [`priority-fulfillment-deploy.yaml`](./priority-fulfillment-deploy.yaml) are a demo class and workload for generating traffic across more than one rule.

## Prerequisites

- **Custom ComputeClasses**: GKE **1.30.3-gke.1451000+**.
- **GKE 1.33+**, where the `ccc_priority_index` annotation starts being written. **Google does not document it** — it is in neither the ComputeClass concepts page nor the CRD reference — so this floor was measured, not quoted:

  | GKE version | Stamps `ccc_priority_index`? |
  | --- | --- |
  | 1.31.14-gke.2630000 | no |
  | 1.32.13-gke.2337000 | no |
  | 1.33.13-gke.1547000 | **yes** |
  | 1.34.10-gke.1328000 | **yes** |
  | 1.36.4 (`-gke.1391000` REGULAR, `-gke.1082000` RAPID) | **yes** |

  Each row is a real cluster running a two-rule class with a pod forcing a scale-up; the negatives were confirmed by dumping *every* annotation on the provisioned node, not just the expected key. On 1.31/1.32 the nodes come up fine — they are simply never annotated, and the exporter degrades on purpose rather than lying (see [Mixed fleets](#mixed-fleets)).

  **1.33 is a low bar, and that is the point.** It sits at or below the minimum supported version of every active release channel, so most clusters already qualify with no upgrade. (`priorityScore`, by contrast, needs 1.35.2+ — which is exactly why this is built on the annotation.)

  **Watch the spelling**: bare `ccc_priority_index`, **no `cloud.google.com/` prefix**. The prefixed form returns nothing on every version and looks identical to an unsupported cluster.

  ```bash
  kubectl get nodes -o json | python3 -c "import json,sys
  for n in json.load(sys.stdin)['items']:
      a = n['metadata'].get('annotations', {})
      print(n['metadata']['name'], a.get('ccc_priority_index', '<absent>'))"
  ```

- **Managed Service for Prometheus** enabled (on by default for Autopilot and recent Standard clusters). The exporter is scraped through a `PodMonitoring`.
- **`roles/monitoring.dashboardEditor`** on the project.

No image build and no registry: the exporter script is mounted from a ConfigMap into a stock `python:3.12-slim` image. The dashboards are pure Cloud Monitoring + PromQL with no GKE dependency at all.

## Why vCPU and not node count

"Nodes provisioned per rule" is misleading the moment your rules provision different shapes. A class falling back from `c3` to `e2` can report a comfortable 50/50 node split while the fallback carries most of the actual compute. Node count answers "how many times," not "how much."

So the headline unit is **vCPU-time**: `ccc_vcpus_by_priority` plotted as a stacked area means **the area under each band is vCPU-hours**, read straight off the chart without a separate calculation. The share tile normalizes that to the number you put in a review — *"87% of our capacity came from rule 0 this week."* `ccc_node_provisions_total` is kept alongside it, because "how many times did we fall back" is a genuinely different question: one large fallback node and twenty small ones are very different events. `ccc_nodes_by_priority` (raw node count) is right for the narrow case where every rule provisions the same shape.

`ccc_accelerators_by_priority` covers the case vCPUs get wrong. On an accelerator class the chip count is what you rank rules over and what dominates the bill, and two rules can deliver identical vCPUs with different chip counts. Verified against a live GPU node: a class whose rule 0 is `n1-standard-2` + 1× T4 reports `ccc_accelerators_by_priority{accelerator="nvidia.com/gpu"} = 1` next to `ccc_vcpus_by_priority = 2` for the same node — exactly the divergence the metric exists to show.

## What the exporter emits

[`exporter.yaml`](./exporter.yaml) deploys a namespace, a ServiceAccount, a ClusterRole granting **read-only access to nodes and nothing else**, the script in a ConfigMap, a single-replica Deployment, and a `PodMonitoring` scraping every 30s. It polls the node API every 10s and, for each node labelled `cloud.google.com/compute-class`, reads the annotation and emits:

| Metric | Type | Meaning |
|---|---|---|
| `ccc_vcpus_by_priority` | gauge | vCPUs running on each rule. Area under the stack = vCPU-hours. |
| `ccc_accelerators_by_priority` | gauge | Chips per rule, labelled `accelerator` (`nvidia.com/gpu`, `google.com/tpu`). Absent on CPU-only fleets. |
| `ccc_node_provisions_total` | counter | Nodes provisioned per rule, deduplicated by node UID. |
| `ccc_nodes_by_priority` | gauge | Raw node count per rule. |
| `ccc_exporter_up` | gauge | 1 when the last poll succeeded. |
| `ccc_priority_annotation_supported` | gauge | 1 if this cluster stamps the annotation, 0 if not, absent if no node is old enough to say. |

Every series carries `computeclass`, `priority` and `kind`. `kind` is one of `rule` (a numeric index), `sentinel` (`ccc_scale_up_anyway`, `ccc_no_rule_matching`, `ccc_deleted`), `pending` (annotated any moment now), or `unsupported` (past the grace window — this cluster does not stamp it at all).

**Multiple clusters come free.** GMP stamps `cluster`, `location` and `project_id` onto every series, so `sum by (cluster) (...)` rolls up with no exporter change — just deploy the same `exporter.yaml` everywhere. Verified across two regions:

```
sum by (cluster,computeclass) (ccc_vcpus_by_priority{kind="rule"})

  ccc-accel            system-pods              2 vCPU     (us-central1)
  observability-test   spot-history-demo       40 vCPU     (asia-east1)
```

Note `system-pods` — a GKE-managed class nobody authored. The fleet view picks up every class in the cluster, which is usually what you want and occasionally a surprise. For clusters in **different projects**, point the dashboard at a [metrics scope](https://cloud.google.com/monitoring/settings) that includes them; the PromQL does not change.

<a name="mixed-fleets"></a>
### Mixed fleets: when only some clusters stamp the annotation

The whole dashboard rests on one undocumented annotation, and any cluster still on 1.31/1.32 runs ComputeClasses perfectly well while never writing it. "No annotation yet" and "no annotation ever" look identical in a single API read, so the exporter separates them **by node age** — see the first correctness note below — and turns that into a cluster-level verdict:

```
ccc_priority_annotation_supported   1  this cluster stamps ccc_priority_index
                                    0  it does not
                              <absent> no node has been up long enough to say
```

Absent is deliberate: "we cannot tell yet" is a different claim from "unsupported," and a cluster that just came up should not light up red for five minutes. On a mixed fleet the share tiles stay honest (every share query is a ratio over `kind="rule"`, so an unsupported cluster contributes to neither numerator nor denominator), the exclusion stays visible (*Clusters NOT stamping the annotation* and *Unattributable vCPUs by cluster*), and provisioning rate still works — you lose attribution, not the dashboard.

## Four correctness notes worth stealing

**The annotation lags the node by about a minute.** GKE writes `ccc_priority_index` *after* the node object, as a separate patch — measured at 57 and 62 seconds, during which the node is `Ready` and scheduling pods. An exporter that treats a missing annotation as rule 0 quietly overstates fulfillment on every scale-up. Within `ANNOTATION_GRACE_SECONDS` (300s — five cycles of the annotator's 1-minute loop) an unannotated node lands in a `pending_annotation` band that drains; past it, in a `no_annotation` band with `kind="unsupported"` that does not. Share tiles filter to `kind="rule"`, so the annotation window is never scored against rule 0; the stacked vCPU chart still shows it, because it is real capacity.

**Managed Prometheus anchors a counter at its first observed value.** GMP stores `ccc_node_provisions_total` as a cumulative series baselined at whatever the exporter already held when GMP first scraped it. Observed live: the exporter reported `priority="0"` at 3 and `priority="1"` at 8 while GMP served 0 and 3. So **the first cohort of nodes on a newly-seen priority is invisible to `increase()`**; everything after registers normally. The tile uses `increase(...[1h])` and should be read as a trend, not an audited total. For the true count, scrape the pod directly — it listens on **9100**:

```bash
kubectl -n ccc-observability exec deploy/ccc-priority-exporter -- \
  python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"
```

The gauge-based tiles — the rule-0 share scorecard and the vCPU-hour chart, the headline numbers — are unaffected, because gauges carry no baseline.

**`priorityScore` inverts what "rule 0" means.** When a rule carries an optional `priorityScore` (1–1000, higher is more preferred), the score rather than list position decides what GKE tries first. Probed with a deliberately inverted class (`e2` score 10 first, `n2` 500, `c2` 900 last), GKE provisioned the `c2` rule and stamped `ccc_priority_index: 2` — **the annotation is the list index, not the preference rank.** On a score-ordered class these tiles would report 0% on rule 0 while the class gets its most-preferred shape every time: an inverted conclusion, not a rounding error. **Use them on list-ordered classes**, which is how the great majority are written; if you set `priorityScore`, read the per-rule chart against your own ordering and ignore the rule-0 scorecard.

**Audit logs will double-count.** The intuitive alternative — a log-based metric over `io.k8s.core.v1.nodes.create` — does not work, because at create time the annotation does not exist yet. Filtering `nodes.patch`/`nodes.update` finds it, but one node's annotation write can appear in both, over-counting provisions by roughly 2× in testing. The exporter deduplicates by node **UID**, and seeds on first poll (existing nodes recorded but not counted) so a restart does not fabricate a burst.

## Why the health view is separate

The three questions GKE's own per-ComputeClass metrics are meant to answer — pods pending on this class, provisioning attempts, and attempts that failed and why — **require GKE 1.37+**. `kubernetes.io/autoscaler/cluster_pending_pods_per_ccc` and the two `..._node_provisioning_*_per_ccc` types are rolling out now and **the exact patch version has not been announced**, so treat `1.37+` as a floor and verify in your own project:

```bash
gcloud monitoring metrics-descriptors list --filter='metric.type~"per_ccc"'
```

Until the rollout lands, all three return `404 NOT_FOUND` — the same response Cloud Monitoring gives for a name that was never defined, and distinct from the `series=0` a real metric with no data returns. That was the state on 1.36.4-gke.1391000 and still the state on a purpose-built **1.37.0-gke.3503000** cluster driven by a class that produced pending pods, one failed attempt (`UnavailableMachineType`) and one success within three minutes: eighteen polls over twelve minutes, eighteen `404`s. There is no `--monitoring` component to turn them on and there never will be — autoscaler metrics ride under `SYSTEM`.

So `dashboard-health.json` answers the first two questions from metrics **verified to carry data today** (`kube_pod_status_unschedulable`, and this exporter's own per-rule provisioning counter) and is explicit on its face that it cannot answer the third. Failure *reasons* still come from `status.priorityStatuses` and pod events. When the per-CCC metrics arrive they slot in beside these tiles rather than replacing them — the exporter's counter is per-rule, which the GKE metric is not.

## What makes these dashboards portable

- **No project, cluster or ComputeClass names in the JSON.** Nothing to find-and-replace before importing, and nothing project-specific to leak back out if you edit one in the console and re-export it.
- **Every template variable defaults to `.*`**, so an imported dashboard shows every class reporting instead of opening blank. **Do not default it to a class name that may not exist yet** — a filter naming a missing class renders "No data is available for the selected time frame" on every tile, which looks exactly like a broken exporter.
- **Mind the variable syntax**: `${computeclass.value}` inside an explicit `=~"..."` matcher, **not** bare `${computeclass}`. The bare form expands to a whole matcher using `=`, so a `.*` default silently becomes `computeclass=".*"` and matches nothing — an empty chart rather than an error. The `.value` form substitutes the value alone, which is what a regex matcher wants.
- **Two data sources, both standard.** `ccc_*` comes from `exporter.yaml`. `kube_pod_status_unschedulable`, behind the health dashboard's pending-pod tiles, comes from GKE's managed kube-state-metrics: `gcloud container clusters update CLUSTER --location LOCATION --monitoring=SYSTEM,POD` (the flag *replaces* the component set, so list everything you want to keep). Both queries end in `or vector(0)` so a healthy cluster reads `0` rather than "No data" — which also means that **without the `POD` package they read a steady, believable zero forever.** The installer's preflight checks for it.
- **Accelerators get their own dashboards**, rather than a second Y axis on the vCPU chart: the units share no scale, and a fleet with no TPUs should show an empty chart, not a legend entry that never plots. The installer skips the GPU and TPU variants when the project reports no chips of that kind (`--all` overrides), because an all-blank dashboard is indistinguishable from a broken exporter.
- **`gcloud monitoring dashboards update` needs an `etag`**, which the committed files deliberately omit — an etag identifies one installed copy, so committing one would pin the JSON to a single project. The installer reads the live etag and splices it in; by hand, delete and recreate instead:

  ```bash
  gcloud monitoring dashboards create --config-from-file=dashboard-fleet.json --project <project-id>
  ```

### Time range is a URL concern, not a dashboard setting

The Cloud Monitoring API has no dashboard-level default time range: `Dashboard` carries no such field, and the only `timeRange` is per-widget, which *overrides* the picker rather than seeding it (and supports line, stacked-area and stacked-bar widgets only, so scorecards would disagree). The console opens every dashboard at its own 1-hour default. Since scale-up behaviour is easier to read over a day, bookmark an explicit duration instead:

```
https://console.cloud.google.com/monitoring/dashboards/builder/<dashboard-id>;duration=PT24H?project=<project-id>

# with the class filter pinned too:
https://console.cloud.google.com/monitoring/dashboards/builder/<dashboard-id>;duration=PT24H;filters=var:computeclass,val:<class-name>?project=<project-id>
```

`duration` takes an ISO-8601 period — `PT1H`, `PT6H`, `PT24H`, `P7D`.

## Generate some traffic across rules

```bash
kubectl apply -f priority-fulfillment-class.yaml
kubectl apply -f priority-fulfillment-deploy.yaml
kubectl scale deploy/priority-fulfillment-load --replicas=8
```

Allow a few minutes: nodes must provision, the annotation must land, and GMP must ingest a scrape or two. Then check what the annotation says per node:

```bash
kubectl get nodes \
  -L cloud.google.com/compute-class \
  -o custom-columns='NODE:.metadata.name,PRIORITY:.metadata.annotations.ccc_priority_index,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type'
```

Or query the fulfillment rate without the dashboard:

```promql
# Share of attributable vCPU-time served by rule 0 over 24h.
# A ratio of sum_over_time, so the scrape interval cancels out.
100 * sum(sum_over_time(ccc_vcpus_by_priority{computeclass="priority-fulfillment",kind="rule",priority="0"}[24h]))
    / sum(sum_over_time(ccc_vcpus_by_priority{computeclass="priority-fulfillment",kind="rule"}[24h]))

# vCPU-hours per rule. Each sample covers 30s, so /120 converts samples to hours.
# Observed capacity-time: gaps count as zero rather than being extrapolated,
# so a young series understates rather than inflates.
sum by (priority) (sum_over_time(ccc_vcpus_by_priority{computeclass="priority-fulfillment",kind="rule"}[24h])) / 120
```

> **The `/120` is tied to the 30s scrape interval** in `exporter.yaml`. If you change `PodMonitoring.spec.endpoints[].interval`, change the divisor to `3600 / <interval seconds>`.

## Reading the result

A healthy class is a solid band of rule 0 with thin slivers above it.

- **A persistent fallback band** means your first-choice shape is not reliably available in that region — either the preference is aspirational, or the rule needs more zones.
- **A fallback band at the same time each day** is contention, not scarcity, and usually argues for a reservation.
- **Anything in the sentinel tile** deserves attention. `ccc_no_rule_matching` means nodes were provisioned matching none of your rules; `ccc_scale_up_anyway` means every rule was exhausted and `whenUnsatisfiable: ScaleUpAnyway` caught the fall. Both mean the class is not describing reality.
- **A wide `pending_annotation` band** is normal during bursts and should drain within a minute. If it does not, the exporter has lost node visibility — check `ccc_exporter_up`.

## Cleanup

```bash
kubectl delete -f priority-fulfillment-deploy.yaml --ignore-not-found
kubectl delete -f priority-fulfillment-class.yaml --ignore-not-found
kubectl delete -f exporter.yaml --ignore-not-found
```

Node auto-provisioning reclaims the nodes a few minutes later, leaving empty `nap-*` node pool shells behind (harmless). Delete dashboards from the console, or with `gcloud monitoring dashboards delete <dashboard-id>`.
