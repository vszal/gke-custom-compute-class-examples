# Storage: boot disk + local SSD

Customize a node's **boot disk** (type/size) and attach **local SSD** through the
`storage` field of a priority rule.

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
- `localSSDCount` attaches that many **raw local SSD** devices (375 GiB each) to the
  node.

[`lssd-deploy.yaml`](./lssd-deploy.yaml) mounts an `emptyDir` at `/cache` as scratch
space.

> **Local SSD vs ephemeral storage — read this.** `localSSDCount` attaches local SSDs
> as **raw block devices**. By default a Kubernetes `emptyDir` is backed by the node
> **boot disk**, *not* these raw SSDs. To make `emptyDir` / ephemeral storage actually
> land on local SSD, the node pool must be configured for *ephemeral-storage-local-ssd*
> (such nodes carry the label `cloud.google.com/gke-ephemeral-storage-local-ssd=true`).
> Treat this example as "attach raw local SSD scratch capacity"; configure
> ephemeral-storage-local-ssd separately if you need `emptyDir` to sit on SSD.

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
