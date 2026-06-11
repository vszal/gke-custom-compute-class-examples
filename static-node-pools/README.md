# Static (pre-created) node pools

*Requires a GKE **Standard** cluster.*

Most examples here rely on node auto-provisioning. This one instead references
**node pools you create yourself** with the `nodepools` priority rule, and shows how
**fallback** and **active migration** behave when a pool hits its `max-nodes` ceiling.

## What this example shows

[`static-pools-class.yaml`](./static-pools-class.yaml) ranks three Spot pools:

```yaml
priorities:
- nodepools: [e2-4-spot-pool]
- nodepools: [n2-4-spot-pool]
- nodepools: [n2d-4-spot-pool]
activeMigration:
  optimizeRulePriority: true
```

There is no `nodePoolAutoCreation` — the pools must already exist. Each is capped at
a small `max-nodes` so you can watch GKE fall back to the next pool when one fills,
and migrate workloads back up when capacity frees.

## 1. Create the node pools

Edit [`create-node-pools.sh`](./create-node-pools.sh) and set `CLUSTER_NAME` and
`LOCATION`, then:

```bash
chmod 750 ./*.sh
./create-node-pools.sh
```

This creates `e2-4-spot-pool` (max 1 node), `n2-4-spot-pool` (max 2), and
`n2d-4-spot-pool` (max 5), each labeled and tainted for the `cost-optimized` class.

## 2. Deploy the class + workload

```bash
kubectl apply -f static-pools-class.yaml
kubectl apply -f static-pools-deploy.yaml   # 10 replicas
```

Watch nodes, pools, and pods (from the repo root, after configuring `status.sh`):

```bash
watch ./status.sh
```

## 3. Scale up to see fallback

```bash
kubectl scale deployment test-workload --replicas 30
```

The e2 pool (max 1) is already full, so new pods land on **n2**. Push further:

```bash
kubectl scale deployment test-workload --replicas 100
```

Once n2 (max 2) fills, pods fall back to **n2d**.

## 4. See active migration

Raise the e2 pool ceiling so higher-priority capacity reopens:

```bash
# set CLUSTER_NAME / LOCATION in this script too
./update-e2-node-pool.sh        # bumps e2-4-spot-pool max-nodes to 10
```

After a bit you'll see new e2 Spot nodes appear and workloads **migrate** off n2/n2d
back onto the preferred e2 pool — that's `activeMigration.optimizeRulePriority` at
work.

## 5. Clean up

```bash
kubectl delete -f static-pools-deploy.yaml   # watch the scale-down
```
