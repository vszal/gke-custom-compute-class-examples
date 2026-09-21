# Priority-rule fulfillment: how often did rule 0 actually win?

A ComputeClass states a preference. `kubectl get computeclass` tells you what is true right now. Neither tells you the thing you actually want to know at review time: **over the last week, how much of the capacity this class delivered came from your first-choice rule, and how much came from the fallbacks?**

This example turns the `ccc_priority_index` node annotation into Prometheus time series and ships a Cloud Monitoring dashboard built on them. The result is a historical, vCPU-normalized view of priority-rule fulfillment.

## Prerequisites

- **Custom ComputeClasses**, which require GKE **1.30.3-gke.1451000+**.
- **GKE 1.33 or newer**, which is where the `ccc_priority_index` node annotation starts being written. This annotation is what the whole dashboard reads, and **Google does not document it** — it appears in neither the ComputeClass concepts page nor the CRD reference — so the floor below was measured, not quoted:

  | GKE version | Stamps `ccc_priority_index`? |
  | --- | --- |
  | 1.31.14-gke.2630000 | no |
  | 1.32.13-gke.2337000 | no |
  | 1.33.13-gke.1547000 | **yes** |
  | 1.34.10-gke.1328000 | **yes** |
  | 1.36.4 (`-gke.1391000` REGULAR, `-gke.1082000` RAPID) | **yes** |

  Each row is a real cluster running a two-rule ComputeClass with a pod forcing a scale-up; the negatives were confirmed by dumping *every* annotation on the provisioned node, not just the expected key. On 1.31/1.32 the nodes come up correctly — they are simply never annotated, so there is nothing for the exporter to read. On such a cluster it degrades on purpose rather than lying — see [Mixed fleets](#mixed-fleets-when-only-some-clusters-stamp-the-annotation).

  **1.33 is a low bar, and that is the point.** The usual objection to a new GKE capability is "great, but we can't upgrade the fleet for it." That does not apply here: 1.33 is at or below the minimum supported version of every active release channel, so most clusters already qualify today and need no upgrade at all to get this dashboard. Only a fleet deliberately held back on 1.31/1.32 is excluded. (The newer `priorityScore` field, by contrast, does need 1.35.2+ — which is exactly why this dashboard is built on the annotation instead.)

  **Watch the key's spelling.** It is bare `ccc_priority_index`, with **no `cloud.google.com/` prefix**. Querying the prefixed form returns nothing on every version and is indistinguishable from an unsupported cluster. Check yours:

  ```bash
  kubectl get nodes -o json | python3 -c "import json,sys
  for n in json.load(sys.stdin)['items']:
      a = n['metadata'].get('annotations', {})
      print(n['metadata']['name'], a.get('ccc_priority_index', '<absent>'))"
  ```

  The dashboards themselves are pure Cloud Monitoring + PromQL and carry no GKE dependency at all.
- **Google Managed Prometheus enabled** on the cluster (it is on by default for Autopilot and for recent Standard clusters). The exporter is scraped through a `PodMonitoring` resource.
- Permission to create a dashboard in the project (`roles/monitoring.dashboardEditor`).

No image build and no registry are required — the exporter script is mounted from a ConfigMap into a stock `python:3.12-slim` image.

## Why vCPU and not node count

The obvious metric is "nodes provisioned per rule," and it is misleading the moment your rules provision different machine shapes. A class that falls back from `c3` to `e2` might report a comfortable 50/50 node split while the fallback is carrying most of the actual compute — or the reverse. Node count answers "how many times," not "how much."

So the headline unit here is **vCPU-time**:

- `ccc_vcpus_by_priority` is a gauge of vCPUs currently running on each rule. Plotted as a stacked area over time, **the area under each band is vCPU-hours** — so "volume" reads directly off the chart, duration-weighted, without a separate calculation.
- The share tile normalizes that to a percentage, which is the number to put in a review: *"87% of our capacity came from rule 0 this week."*
- `ccc_node_provisions_total` is kept alongside it as a counter, because "how many times did we have to fall back" is a genuinely different question from "how much capacity came from the fallback." A single large fallback node and twenty small ones are very different events.

`ccc_nodes_by_priority` (raw node count) is also exported, and it is the right metric for the narrow case where every rule in a class provisions the same shape.

`ccc_accelerators_by_priority` covers the case vCPUs get wrong. On an accelerator class the chip count, not the core count, is what you are ranking rules over and what dominates the bill — and two rules can deliver the same vCPUs while delivering different numbers of chips. It carries an `accelerator` label (`nvidia.com/gpu`, `google.com/tpu`) and is emitted only for rules actually running accelerators, so a CPU-only fleet sees nothing. It plots on the **right-hand axis** of the capacity tile, because a handful of chips against hundreds of vCPUs on one scale flattens to a line on the floor. Verified against a live GPU node: a two-rule class whose rule 0 is `n1-standard-2` + 1× T4 on spot reports `ccc_accelerators_by_priority{priority="0",accelerator="nvidia.com/gpu"} = 1` next to `ccc_vcpus_by_priority = 2` for the same node — which is exactly the divergence the metric exists to show.

## What this example shows

1. [`exporter.yaml`](./exporter.yaml) deploys the exporter: a namespace, a ServiceAccount, a ClusterRole granting **read-only access to nodes and nothing else**, the script in a ConfigMap, a single-replica Deployment, and a `PodMonitoring` that scrapes it every 30s.

   It polls the node API every 10s, and for each node carrying the `cloud.google.com/compute-class` label it reads the `ccc_priority_index` annotation and emits:

   | Metric | Type | Meaning |
   |---|---|---|
   | `ccc_vcpus_by_priority` | gauge | vCPUs running on each rule. Area under the stack = vCPU-hours. |
   | `ccc_node_provisions_total` | counter | Nodes provisioned per rule, deduplicated by node UID. |
   | `ccc_nodes_by_priority` | gauge | Raw node count per rule. |
   | `ccc_accelerators_by_priority` | gauge | Accelerator chips per rule, labelled by `accelerator` (`nvidia.com/gpu`, `google.com/tpu`). Absent on CPU-only fleets. |
   | `ccc_exporter_up` | gauge | 1 when the last poll succeeded. |
   | `ccc_priority_annotation_supported` | gauge | 1 if this cluster stamps the annotation, 0 if it does not, absent if no node is old enough to say. |

   Every series carries `computeclass`, `priority`, and `kind` labels. `kind` is one of `rule` (a numeric priority index), `sentinel` (`ccc_scale_up_anyway`, `ccc_no_rule_matching`, `ccc_deleted`), `pending` (annotated any moment now), or `unsupported` (past the grace window — this cluster does not stamp it at all) — see the correctness notes below, which are the whole reason this is an exporter rather than a log-based metric.

2. [`dashboard.json`](./dashboard.json) is an importable Cloud Monitoring dashboard with seven tiles: a rule-0 share scorecard, a fallback vCPU-hours scorecard, provisioning events per rule, the stacked vCPU area chart, a 100%-stacked share chart, node counts, and a sentinel/unannotated tile. A `computeclass` template variable at the top switches between classes.

3. [`dashboard-fleet.json`](./dashboard-fleet.json) is the **fleet roll-up**: the same data aggregated across every ComputeClass and every cluster, for capacity planning and cost review rather than debugging one class. Thirteen tiles — fleet rule-0 share, fleet vCPU-hours, fallback vCPU-hours, a count of reporting classes, vCPU by rule, **vCPU by ComputeClass** (cost attribution), **rule-0 share by ComputeClass** (which classes are missing their preferred shape), the same two broken out **by cluster**, and three tiles for fleet trust — clusters reporting, **clusters not stamping the annotation**, and **unattributable vCPUs by cluster**. Both `computeclass` and `cluster` are template variables defaulting to `.*`, so it opens fleet-wide and narrows on click.

   Note the variable syntax: `${computeclass.value}` inside an explicit `=~"..."` matcher, **not** bare `${computeclass}`. The bare form expands to a whole label matcher using `=`, so a `.*` default silently becomes `computeclass=".*"` and matches nothing — an empty chart rather than an error. The `.value` form substitutes the value alone, which is what a regex matcher wants.

4. [`dashboard-health.json`](./dashboard-health.json) is the **scale-up health** view — a different axis from the two above. Where `dashboard.json` and `dashboard-fleet.json` ask *what shape did I get*, this one asks *is provisioning working right now, and can I trust what I'm reading*. Eight tiles: three health scorecards across the top (unschedulable pods, whether priority attribution is available on this cluster, whether the exporter is alive), unschedulable pods over time, node provisions per rule in 5-minute buckets, nodes by rule, fallback pressure as a percentage, and a text tile that states the dashboard's own blind spots.

   Every query in it was executed against live Managed Prometheus before it shipped, so no tile rests on a metric that merely appears in documentation.

5. [`priority-fulfillment-class.yaml`](./priority-fulfillment-class.yaml) and [`priority-fulfillment-deploy.yaml`](./priority-fulfillment-deploy.yaml) are a demo class and workload for generating traffic across more than one rule.

## Three levels of granularity

The same exporter and the same metrics serve all three; only the aggregation changes.

**One ComputeClass** (`dashboard.json`) answers *is this class getting the shape I asked for?* This is the debugging view — you look at it when a team says their workload is on the wrong machines.

**The fleet** (`dashboard-fleet.json`) answers *across everything we run, how much capacity is landing on preferred rules, and which classes are dragging?* Because every series carries a `computeclass` label, `sum by (computeclass)` gives per-class cost attribution for free, and `100 * rule-0 vCPU-hours / total vCPU-hours by class` ranks the offenders. This is the FinOps view.

### Why the health view exists separately

The three questions GKE's own per-ComputeClass metrics are meant to answer — how many pods are pending on this class, how many provisioning attempts were made, and how many failed and why — **require GKE 1.37+**. The metric types `kubernetes.io/autoscaler/cluster_pending_pods_per_ccc`, `cluster_node_provisioning_attempts_count_per_ccc`, and `cluster_node_provisioning_failed_attempts_count_per_ccc` are rolling out now; **the exact patch version has not been announced yet**, so treat `1.37+` as the floor and verify against your own project before you plan around them. Until the rollout reaches a project, querying any of the three returns `404 NOT_FOUND` — the same response Cloud Monitoring gives for a metric name that was never defined, and distinct from the `series=0` it returns for a real metric type that simply has no data. That was the state on a 1.36.4-gke.1391000 cluster with SYSTEM monitoring and Managed Prometheus enabled, actively autoscaling ComputeClass node pools, and still the state on a purpose-built **1.37.0-gke.3503000** cluster (newest RAPID version at the time) driven by a two-rule class that produced pending pods, one failed provisioning attempt (`UnavailableMachineType`), and one successful one within three minutes — eighteen polls over twelve minutes, eighteen `404`s. There is no `--monitoring` component to turn them on and there never will be: the component list has no autoscaler entry, because autoscaler metrics ride under `SYSTEM`. Check for yourself with `gcloud monitoring metrics-descriptors list --filter='metric.type~"per_ccc"'`.

Everything on this dashboard is built on metrics **verified to carry data today**, so it keeps working before and after that rollout lands. When the per-CCC metrics do arrive, they add the one thing this view genuinely cannot supply — a per-class failure *reason* — rather than replacing anything here.

So `dashboard-health.json` answers the first two questions from metrics that do exist (`kube_pod_status_unschedulable` from kube-state-metrics, and this exporter's own `ccc_node_provisions_total`), and is explicit on its face that it cannot answer the third. Failure *reasons* still come from `status.priorityStatuses` and pod events. When the per-CCC metrics do land, they slot in beside these tiles rather than replacing them — the exporter's provisioning counter is per-rule, which the GKE metric is not.

**Multiple clusters** come free with Managed Prometheus. GMP stamps `cluster`, `location`, and `project_id` onto every series as resource labels, so `sum by (cluster) (...)` rolls up without any change to the exporter — you just deploy the same `exporter.yaml` to each cluster. Verified across two clusters in different regions:

```
sum by (cluster,computeclass) (ccc_vcpus_by_priority{kind="rule"})

  ccc-accel            system-pods              2 vCPU     (us-central1)
  observability-test   spot-history-demo       40 vCPU     (asia-east1)
```

Note `system-pods` there — a GKE-managed ComputeClass nobody authored. The fleet view picks up every class in the cluster, not just yours, which is usually what you want for capacity planning and occasionally a surprise.

For clusters in **different projects**, point the dashboard at a [metrics scope](https://cloud.google.com/monitoring/settings) that includes them; the PromQL does not change. Within one project it already works.

### Mixed fleets: when only some clusters stamp the annotation

A fleet is rarely uniform, and the whole dashboard rests on one undocumented annotation. Any
cluster still on 1.31 or 1.32 is exactly this case: it runs ComputeClasses perfectly well and
never writes the annotation. So the exporter is built to tell you which clusters it can
actually speak for.

The hard part is that "no annotation yet" and "no annotation ever" look identical in a single
read of the API. The exporter separates them **by node age**. Within a grace window
(`ANNOTATION_GRACE_SECONDS`, 300s — the measured stamping lag is 57–62s, and the annotator's
own loop in the cluster-autoscaler source runs on a 1-minute interval, so 300s is five cycles)
an unannotated node
is simply young, and lands in the `pending_annotation` band, which drains. Past that window
it is not waiting for anything, and lands in a distinct `no_annotation` band with
`kind="unsupported"`, which never drains.

That split drives a cluster-level verdict:

```
ccc_priority_annotation_supported   1  this cluster stamps ccc_priority_index
                                    0  it does not
                              <absent> no node has been up long enough to say
```

Absent is deliberate. "We cannot tell yet" is a different claim from "unsupported", and a
fleet tile should not flatten the two — a cluster that just came up would otherwise light up
red for five minutes.

What this buys you on a mixed fleet:

- **The share tiles stay honest.** Every rule-0-share query is a ratio over `kind="rule"`, so
  an unsupported cluster contributes to neither numerator nor denominator. Its capacity cannot
  drag the fleet number down, and cannot silently inflate it either.
- **The exclusion is visible.** The *Clusters NOT stamping the annotation* scorecard goes red
  above zero, and *Unattributable vCPUs by cluster* shows exactly how much capacity is outside
  the share math. Without those two tiles, a quietly-excluded cluster is the dangerous case.
- **You still get provisioning rate.** `ccc_node_provisions_total` counts `no_annotation` nodes
  under that priority, so an unsupported cluster still reports *how often* it scales, just not
  *which rule* won.
- **Nothing errors.** No tile breaks, no query fails; you lose attribution, not the dashboard.

## Four correctness notes worth stealing

These are the details that make the difference between a dashboard and a dashboard you can trust.

**The annotation lags the node by about a minute.** GKE writes `ccc_priority_index` *after* the node object is created, as a separate patch. Measured on real scale-ups, the gap was 57 and 62 seconds — and the node is `Ready` and scheduling pods that entire time. A naive exporter that treats a missing annotation as rule 0 will quietly overstate your fulfillment rate every single scale-up. This exporter puts those nodes in their own `pending_annotation` band, and the dashboard's *share* tiles filter to `kind="rule"` so the annotation window is never scored against rule 0. The stacked vCPU chart still shows it, because it is real capacity.

**Managed Prometheus anchors a counter at its first observed value.** `ccc_node_provisions_total` is a counter, and Cloud Monitoring stores it as a cumulative series whose baseline is whatever the exporter already held the first time GMP scraped that series. Observed on a live cluster: the exporter reported `priority="0"` at 3 and `priority="1"` at 8, while GMP served 0 and 3 respectively — `priority="1"` had first been seen at 5, and `priority="0"` was first seen at 3 and so reads as zero. The practical consequence is that **the first cohort of nodes on a newly-seen priority is invisible to `increase()`**; every provision after that registers normally. The provisioning tile therefore uses `increase(...[1h])` rather than the raw counter, and you should read it as a trend, not an audited total. If you need the true count, scrape the pod directly — note it listens on **9100**:

```bash
kubectl -n ccc-observability exec deploy/ccc-priority-exporter -- \
  python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"
```

The gauge-based tiles — the rule-0 share scorecard and the vCPU-hour chart, which are the headline numbers — are unaffected, because gauges carry no baseline.

**`priorityScore` inverts what "rule 0" means.** A priority rule may carry an optional `priorityScore` (1–1000, higher is more preferred, at most three rules sharing a value). When it is set, the score — not list position — decides which rule GKE tries first, and the two can point in opposite directions. Probed on a live cluster with a deliberately inverted class (`e2` score 10 first, `n2` score 500, `c2` score 900 last), GKE provisioned the `c2` rule and stamped `ccc_priority_index: 2`: **the annotation is the list index, not the preference rank.** So on a score-ordered class this dashboard would report 0% on rule 0 while the class is in fact getting its most-preferred shape every time — an inverted conclusion, not a rounding error. **Use these tiles on list-ordered classes**, which is how the great majority are written; if you set `priorityScore`, read the per-rule vCPU chart against your own score ordering and ignore the rule-0 scorecard.

**Audit logs will double-count.** The intuitive alternative to an exporter is a log-based metric over `io.k8s.core.v1.nodes.create`. That does not work: at create time the annotation does not exist yet. Filtering on `nodes.patch`/`nodes.update` finds it, but a single node's annotation write can appear in both, which over-counted provisions by roughly 2x in testing. The exporter deduplicates by node **UID** instead. It also seeds on first poll — existing nodes are recorded but not counted — so restarting the exporter does not fabricate a burst of provisions.

## Deploy

```bash
kubectl apply -f exporter.yaml
kubectl -n ccc-observability rollout status deploy/ccc-priority-exporter
```

Confirm it is producing metrics:

```bash
kubectl -n ccc-observability port-forward deploy/ccc-priority-exporter 9100:9100 &
curl -s localhost:9100/metrics | grep -v '^#'
```

Import the dashboards (each is a separate dashboard; import whichever you want):

```bash
gcloud monitoring dashboards create --config-from-file=dashboard.json        --project <project-id>
gcloud monitoring dashboards create --config-from-file=dashboard-fleet.json  --project <project-id>
gcloud monitoring dashboards create --config-from-file=dashboard-health.json --project <project-id>
```

Each ships with the `computeclass` template variable defaulting to `priority-fulfillment`, the demo class below. Pointing one at your own class is a one-field edit in the dashboard's filter bar — or change `stringValue` in the JSON before importing.

Generate some traffic across rules:

```bash
kubectl apply -f priority-fulfillment-class.yaml
kubectl apply -f priority-fulfillment-deploy.yaml
kubectl scale deploy/priority-fulfillment-load --replicas=8
```

Allow a few minutes: nodes must provision, the annotation must land, and Managed Prometheus must ingest a scrape or two.

## Observe

Check what the annotation says per node:

```bash
kubectl get nodes \
  -L cloud.google.com/compute-class \
  -o custom-columns='NODE:.metadata.name,PRIORITY:.metadata.annotations.ccc_priority_index,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type'
```

Query the fulfillment rate directly, without the dashboard:

```promql
# Share of attributable vCPU-time served by rule 0 over 24h.
# A ratio of sum_over_time, so the scrape interval cancels out.
100 * sum(sum_over_time(ccc_vcpus_by_priority{computeclass="priority-fulfillment",kind="rule",priority="0"}[24h]))
    / sum(sum_over_time(ccc_vcpus_by_priority{computeclass="priority-fulfillment",kind="rule"}[24h]))
```

```promql
# vCPU-hours per rule. Each sample covers 30s, so /120 converts samples to hours.
# This measures observed capacity-time: gaps count as zero rather than being
# extrapolated, so a young series understates rather than inflates.
sum by (priority) (sum_over_time(ccc_vcpus_by_priority{computeclass="priority-fulfillment",kind="rule"}[24h])) / 120
```

> **The `/120` is tied to the 30s scrape interval** in `exporter.yaml`. If you change `PodMonitoring.spec.endpoints[].interval`, change the divisor to `3600 / <interval seconds>`.

## Reading the result

A healthy class is a solid band of rule 0 with thin slivers above it. What to look for:

- **A persistent fallback band** means your first-choice shape is not reliably available in that region — either the preference is aspirational, or the rule needs more zones.
- **A fallback band that appears at the same time each day** is contention, not scarcity, and usually argues for a reservation.
- **Anything in the sentinel tile** deserves attention. `ccc_no_rule_matching` means nodes were provisioned that match none of your rules; `ccc_scale_up_anyway` means every rule was exhausted and `whenUnsatisfiable: ScaleUpAnyway` caught the fall. Both mean the class is not describing reality.
- **A wide `pending_annotation` band** is normal during scale-up bursts and should drain within a minute or so. If it does not drain, the exporter has lost node visibility — check `ccc_exporter_up`.

## Cleanup

```bash
kubectl delete -f priority-fulfillment-deploy.yaml --ignore-not-found
kubectl delete -f priority-fulfillment-class.yaml --ignore-not-found
kubectl delete -f exporter.yaml --ignore-not-found
```

Node auto-provisioning reclaims the nodes a few minutes later, leaving empty `nap-*` node pool shells behind (harmless). Delete the dashboards from the Cloud Monitoring console, or with `gcloud monitoring dashboards delete <dashboard-id>` for each one you imported.
