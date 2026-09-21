#!/usr/bin/env python3
"""Generate the GPU and TPU fleet dashboards from the vCPU one.

The three fleet dashboards ask the same questions of three different units of
capacity, so only one of them is written by hand. `dashboard-fleet.json` is the
source of truth; this script rewrites it into the accelerator variants and
writes `dashboard-fleet-gpu.json` and `dashboard-fleet-tpu.json`.

Run it after any edit to dashboard-fleet.json:

    ./make-fleet-variants.py          # regenerate
    ./make-fleet-variants.py --check  # fail if the generated files are stale

The transform is narrow on purpose. `ccc_vcpus_by_priority` becomes
`ccc_accelerators_by_priority` with an `accelerator=` selector pinned on; every
other metric the dashboard reads — `ccc_nodes_by_priority`, `ccc_exporter_up`,
`ccc_priority_annotation_supported` — counts nodes or clusters rather than
capacity, so it is unit-independent and passes through untouched.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
SOURCE = HERE / "dashboard-fleet.json"

VARIANTS = {
    "gpu": {
        "out": "dashboard-fleet-gpu.json",
        "resource": "nvidia.com/gpu",
        "display": "ComputeClass fulfillment — fleet (GPU)",
        # Longest first: "vCPU-hours" must not be half-matched by "vCPU".
        "words": [("vCPU-hours", "GPU chip-hours"),
                  ("vCPUs", "GPU chips"),
                  ("vCPU", "GPU chip")],
    },
    "tpu": {
        "out": "dashboard-fleet-tpu.json",
        "resource": "google.com/tpu",
        "display": "ComputeClass fulfillment — fleet (TPU)",
        "words": [("vCPU-hours", "TPU chip-hours"),
                  ("vCPUs", "TPU chips"),
                  ("vCPU", "TPU chip")],
    },
}

# Capacity metric -> accelerator metric. The `{` is part of the match so the
# selector can be spliced in as the first label; every use in the source
# dashboard has at least one label, which the trailing comma relies on.
VCPU_METRIC = "ccc_vcpus_by_priority{"


def rewrite_query(q: str, resource: str) -> str:
    return q.replace(
        VCPU_METRIC,
        'ccc_accelerators_by_priority{accelerator="%s",' % resource)


def rewrite_text(s: str, words) -> str:
    for old, new in words:
        s = s.replace(old, new)
    return s


def walk(node, resource, words):
    """Rewrite queries and human-readable strings in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "prometheusQuery" and isinstance(value, str):
                node[key] = rewrite_query(value, resource)
            elif key in ("title", "label", "displayName") and isinstance(value, str):
                node[key] = rewrite_text(value, words)
            else:
                walk(value, resource, words)
    elif isinstance(node, list):
        for item in node:
            walk(item, resource, words)


def build(spec):
    d = json.loads(SOURCE.read_text())
    walk(d, spec["resource"], spec["words"])
    d["displayName"] = spec["display"]
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


def main():
    check = "--check" in sys.argv
    stale = []
    for name, spec in VARIANTS.items():
        path = HERE / spec["out"]
        text = build(spec)
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(spec["out"])
        else:
            path.write_text(text)
            print("wrote %s" % spec["out"])
    if check:
        if stale:
            print("stale (re-run %s): %s" % (pathlib.Path(__file__).name,
                                             ", ".join(stale)), file=sys.stderr)
            return 1
        print("generated dashboards are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
