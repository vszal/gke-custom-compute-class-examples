# Storage: boot disk + local SSD

Customize a node's **boot disk** (type/size) and attach **local SSD** through the
`storage` field of a priority rule.

> **Requires GKE 1.30.3-gke.1639000+.** `storage.bootDiskType` / `storage.bootDiskSize`
> carry two different floors: 1.34.1-gke.1431000+ on `machineFamily` priority rules, and
> 1.30.3-gke.1639000+ on every other rule type. This example pins exact `machineType`
> shapes, so the lower floor applies. `storage.localSSDCount` states no requirement.

## What this example shows

[`lssd-class.yaml`](./lssd-class.yaml) provisions c3 (then c3d) nodes with a tuned
boot disk and raw local SSDs:

```yaml
priorities:
- machineType: c3-standard-8-lssd
  storage:
    bootDiskType: pd-balanced
    bootDiskSize: 250        # GiB
    localSSDCount: 2
- machineType: c3d-standard-8-lssd
  storage:
    bootDiskType: pd-balanced
    bootDiskSize: 250
    localSSDCount: 1
```

- `bootDiskType` / `bootDiskSize` set the node's OS/boot disk (here a 250 GiB
  `pd-balanced` disk).
- `localSSDCount` attaches that many **local SSD** devices (375 GiB each) to the
  node.

[`lssd-deploy.yaml`](./lssd-deploy.yaml) mounts an `emptyDir` at `/cache` as scratch
space.

> **Check where `emptyDir` actually lives.** On a self-managed node pool, local SSD
> backs ephemeral storage only when the pool is created for it
> (`--ephemeral-storage-local-ssd`). On GKE 1.37, nodes auto-created from this class
> come up that way on their own: they carry the label
> `cloud.google.com/gke-ephemeral-storage-local-ssd=true` and their allocatable ephemeral
> storage is the local SSD's size, so `emptyDir` lands on SSD. The [ComputeClass reference](https://docs.cloud.google.com/kubernetes-engine/docs/reference/crds/computeclass)
> doesn't state this for `localSSDCount`, so verify on your version before relying on it:
>
> ```bash
> kubectl get nodes -l cloud.google.com/compute-class=c3-lssd \
>   -o custom-columns='NAME:.metadata.name,LSSD:.metadata.labels.cloud\.google\.com/gke-ephemeral-storage-local-ssd,EPHEMERAL:.status.allocatable.ephemeral-storage'
> ```

## Deploy

```bash
kubectl apply -f lssd-class.yaml
kubectl apply -f lssd-deploy.yaml
```

## Observe

```bash
watch ../status.sh    # after configuring status.sh (see the top-level README)

# confirm local SSDs are attached to the node:
kubectl describe node <node-name> | grep -i ssd
```
