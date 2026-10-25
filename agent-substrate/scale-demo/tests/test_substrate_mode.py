"""Unit tests for substrate mode: per-family worker spill planning and runtime config."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import server
from app.server import plan_family_spill, summarize_worker_pods

FAMS = ["c4", "n4", "c3"]
NOW = 10_000.0


def plan(target, placed=None, unscheduled=None, oldest=None, blocked=None, now=NOW):
    return plan_family_spill(
        target,
        FAMS,
        placed or {},
        unscheduled or {},
        oldest or {},
        blocked or {},
        now,
        spill_after=180,
        cooldown=300,
    )


def test_new_demand_goes_to_top_family():
    alloc, blocked = plan(10)
    assert alloc == {"c4": 10, "n4": 0, "c3": 0}
    assert blocked == {}


def test_pending_within_grace_stays_put():
    alloc, blocked = plan(10, unscheduled={"c4": 10}, oldest={"c4": NOW - 100})
    assert alloc == {"c4": 10, "n4": 0, "c3": 0}
    assert blocked == {}


def test_stuck_family_keeps_placed_and_spills_rest():
    alloc, blocked = plan(
        10, placed={"c4": 4}, unscheduled={"c4": 6}, oldest={"c4": NOW - 200}
    )
    assert alloc == {"c4": 4, "n4": 6, "c3": 0}
    assert blocked["c4"] == NOW + 300


def test_cascades_past_two_blocked_families():
    alloc, _ = plan(
        8,
        placed={"c4": 2, "n4": 1},
        blocked={"c4": NOW + 50, "n4": NOW + 50},
    )
    assert alloc == {"c4": 2, "n4": 1, "c3": 5}


def test_all_blocked_last_family_takes_remainder():
    alloc, _ = plan(6, blocked={f: NOW + 50 for f in FAMS})
    assert alloc == {"c4": 0, "n4": 0, "c3": 6}


def test_placed_workers_are_sticky_after_cooldown():
    # c4 recovered, but the 6 workers already on n4 are not moved back up.
    alloc, _ = plan(12, placed={"c4": 4, "n4": 6}, blocked={"c4": NOW - 1})
    assert alloc == {"c4": 6, "n4": 6, "c3": 0}


def test_scale_down_drops_unscheduled_then_bottom_first():
    alloc, _ = plan(
        5,
        placed={"c4": 3, "n4": 2, "c3": 2},
        unscheduled={"c4": 2},
        oldest={"c4": NOW - 10},
    )
    assert alloc == {"c4": 3, "n4": 2, "c3": 0}


def test_scale_to_zero():
    alloc, _ = plan(0, placed={"c4": 3, "n4": 2}, unscheduled={"c3": 1}, oldest={"c3": NOW})
    assert alloc == {"c4": 0, "n4": 0, "c3": 0}


def _pod(pool, scheduled, created="2026-10-24T12:00:00Z", deleting=False):
    meta = {"labels": {"ate.dev/worker-pool": pool}, "creationTimestamp": created}
    if deleting:
        meta["deletionTimestamp"] = created
    return {
        "metadata": meta,
        "status": {
            "conditions": [
                {"type": "PodScheduled", "status": "True" if scheduled else "False"}
            ]
        },
    }


def test_summarize_worker_pods():
    pools = {"microvm-c4": "c4", "microvm-n4": "n4"}
    pods = [
        _pod("microvm-c4", True),
        _pod("microvm-c4", False, "2026-10-24T12:00:30Z"),
        _pod("microvm-c4", False, "2026-10-24T12:00:10Z"),
        _pod("microvm-n4", True, deleting=True),
        _pod("gvisor-c4", True),
    ]
    placed, unscheduled, oldest = summarize_worker_pods(pods, pools)
    assert placed == {"c4": 1, "n4": 0}
    assert unscheduled == {"c4": 2, "n4": 0}
    assert oldest["c4"] == 1792843210.0
    assert "n4" not in oldest


def test_runtime_config_substrate_families(monkeypatch):
    monkeypatch.setattr(server, "DEMO_MODE", "substrate")
    mv = server.get_runtime_config("microvm")
    gv = server.get_runtime_config("gvisor")
    assert mv["families"] == ["c4", "n4", "c3"]
    assert mv["workerPools"]["n4"] == "microvm-n4"
    assert gv["families"] == ["c4", "c4d", "n4", "n4d", "c3", "c3d"]
    assert gv["workerPools"]["c3d"] == "gvisor-c3d"


def test_runtime_config_pause_unchanged():
    cfg = server.get_runtime_config("microvm")
    assert cfg["families"] == server.DEFAULT_FAMILIES
    assert cfg["workerPools"] == {}
    assert cfg["deployment"] == "agent-substrate-microvm-workers"


def _event(pod, reason, ts):
    return {"reason": reason, "involvedObject": {"kind": "Pod", "name": pod}, "lastTimestamp": ts}


def test_latest_scale_up_verdicts_keeps_newest():
    events = [
        _event("w1", "NotTriggerScaleUp", "2026-10-24T12:00:00Z"),
        _event("w1", "TriggeredScaleUp", "2026-10-24T12:01:00Z"),
        _event("w2", "TriggeredScaleUp", "2026-10-24T12:00:00Z"),
        _event("w2", "NotTriggerScaleUp", "2026-10-24T12:02:00Z"),
        _event("w3", "Scheduled", "2026-10-24T12:03:00Z"),
    ]
    v = server.latest_scale_up_verdicts(events)
    assert v["w1"][0] == "TriggeredScaleUp"
    assert v["w2"][0] == "NotTriggerScaleUp"
    assert "w3" not in v


def test_inflight_scale_up_does_not_start_spill_timer():
    pools = {"gvisor-c4": "c4"}
    pods = [_pod("gvisor-c4", False), _pod("gvisor-c4", False)]
    pods[0]["metadata"]["name"] = "inflight"
    pods[1]["metadata"]["name"] = "capped"
    now = 1792843200.0 + 400
    verdicts = {
        "inflight": ("TriggeredScaleUp", now - 30),
        "capped": ("NotTriggerScaleUp", now - 30),
    }
    _, unscheduled, oldest = summarize_worker_pods(pods, pools, verdicts, now)
    assert unscheduled == {"c4": 2}
    assert oldest["c4"] == 1792843200.0  # from "capped" only

    _, _, oldest = summarize_worker_pods(pods[:1], pools, verdicts, now)
    assert "c4" not in oldest

    # A trigger that never produced a node eventually counts as stuck.
    _, _, oldest = summarize_worker_pods(pods[:1], pools, verdicts, now + 1000)
    assert oldest["c4"] == 1792843200.0


class _FakeCustom:
    def __init__(self, replicas):
        self.items = [
            {"metadata": {"name": name}, "spec": {"replicas": n}} for name, n in replicas.items()
        ]

    def list_namespaced_custom_object(self, **_):
        return {"items": self.items}


def test_adopt_cluster_target_after_restart(monkeypatch):
    monkeypatch.setattr(server, "demo_state", {"actorsPerWorker": 1, "sandboxRuntime": "microvm"})
    server._adopt_cluster_target(
        _FakeCustom({"gvisor-c4": 22, "gvisor-c4d": 88, "microvm-c4": 0, "other": 50})
    )
    assert server.demo_state["sandboxRuntime"] == "gvisor"
    assert server.demo_state["targetWorkers"] == 110
    assert server.demo_state["targetAgents"] == 110


def test_adopt_cluster_target_empty_cluster(monkeypatch):
    monkeypatch.setattr(server, "demo_state", {"actorsPerWorker": 1, "sandboxRuntime": "microvm"})
    server._adopt_cluster_target(_FakeCustom({}))
    assert server.demo_state["sandboxRuntime"] == "microvm"
    assert server.demo_state["targetWorkers"] == 0
