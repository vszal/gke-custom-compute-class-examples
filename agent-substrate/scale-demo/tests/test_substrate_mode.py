"""Unit tests for substrate mode: per-family worker spill planning and runtime config."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import server
from datetime import datetime, timezone

from app.server import (
    derive_family_blocks,
    family_node_vcpus,
    parse_ladder_status,
    plan_family_spill,
    quota_full_families,
    summarize_worker_pods,
)

FAMS = ["c4", "n4", "c3"]


def plan(target, placed=None, unscheduled=None, blocked=()):
    return plan_family_spill(target, FAMS, placed or {}, unscheduled or {}, set(blocked))


def test_new_demand_goes_to_top_family():
    assert plan(10) == {"c4": 10, "n4": 0, "c3": 0}


def test_blocked_family_keeps_placed_and_spills_rest():
    alloc = plan(10, placed={"c4": 4}, unscheduled={"c4": 6}, blocked={"c4"})
    assert alloc == {"c4": 4, "n4": 6, "c3": 0}


def test_cascades_past_two_blocked_families():
    alloc = plan(8, placed={"c4": 2, "n4": 1}, blocked={"c4", "n4"})
    assert alloc == {"c4": 2, "n4": 1, "c3": 5}


def test_all_blocked_last_family_takes_remainder():
    assert plan(6, blocked=set(FAMS)) == {"c4": 0, "n4": 0, "c3": 6}


def test_placed_workers_are_sticky_once_unblocked():
    # c4 recovered, but the 6 workers already on n4 are not moved back up.
    assert plan(12, placed={"c4": 4, "n4": 6}) == {"c4": 6, "n4": 6, "c3": 0}


def test_scale_down_drops_unscheduled_then_bottom_first():
    alloc = plan(5, placed={"c4": 3, "n4": 2, "c3": 2}, unscheduled={"c4": 2})
    assert alloc == {"c4": 3, "n4": 2, "c3": 0}


def test_scale_to_zero():
    assert plan(0, placed={"c4": 3, "n4": 2}, unscheduled={"c3": 1}) == {"c4": 0, "n4": 0, "c3": 0}


# --- ComputeClass status + CapacityQuota -> blocks -------------------------------------

IN_PROGRESS = {
    "type": "NodeProvisioningInProgress",
    "status": "True",
    "reason": "PodPending",
    "message": "NodeProvisioning associated with this priority triggered due to pending pods. "
    "2 new nodes will be added with config: {NodePool: nap-c3-standard-4-1t6hv0a7, "
    "MachineType: c3-standard-4, Zones: us-central1-c}",
}
BACKOFF_UNTIL = "2026-10-26 13:47:05"
BACKOFF_EPOCH = datetime(2026, 10, 26, 13, 47, 5, tzinfo=timezone.utc).timestamp()
OUT_OF_RESOURCES = {
    "type": "ProvisioningConstrained",
    "status": "True",
    "reason": "OutOfResources",
    "message": "NodeProvisioning of the node pools associated with this priority failed due "
    f"to the OutOfResources error. In backoff until {BACKOFF_UNTIL} +0000 UTC m=+1234.5",
}
CAPACITY_CONSTRAINED = {
    "type": "ProvisioningConstrained",
    "status": "True",
    "reason": "CapacityConstrained",
    "message": "A subset (1/6) of configurations are excluded due to active capacity cooldowns",
}
SUSPENDED = {
    "type": "ProvisioningSuspended",
    "status": "True",
    "reason": "Backoff",
    "message": "Provisioning for this priority is suspended",
}
T0 = 1_000_000.0


def cc_idx(idx, *conds):
    return {"status": {"priorityStatuses": [{"identifier": str(idx), "conditions": list(conds)}]}}


def blocks(cc_obj=None, quota_full=(), unscheduled=None, oldest=None, prior=None, now=T0):
    return derive_family_blocks(
        FAMS,
        parse_ladder_status(cc_obj, FAMS),
        set(quota_full),
        unscheduled if unscheduled is not None else {"c4": 5},
        oldest or {},
        prior or {},
        now,
        spill_after=120,
        cooldown=300,
    )


def test_in_progress_family_is_not_blocked_even_when_old():
    b = blocks(cc_idx(0, IN_PROGRESS), oldest={"c4": T0 - 3000})
    assert b == {}


def test_out_of_resources_blocks_until_parsed_timestamp():
    now = BACKOFF_EPOCH - 100
    b = blocks(cc_idx(0, OUT_OF_RESOURCES), now=now)
    assert b == {"c4": (BACKOFF_EPOCH, "backoff")}
    # Workers spilled (none unscheduled): block persists until expiry, then lifts.
    again = blocks(cc_idx(0, OUT_OF_RESOURCES), unscheduled={}, prior=b, now=now + 50)
    assert again["c4"][0] == BACKOFF_EPOCH
    gone = blocks(None, unscheduled={}, prior=b, now=BACKOFF_EPOCH + 1)
    assert gone == {}


def test_backoff_needs_unscheduled_workers():
    assert blocks(cc_idx(0, OUT_OF_RESOURCES), unscheduled={}, now=BACKOFF_EPOCH - 100) == {}


def test_identifier_maps_to_ladder_position():
    b = blocks(cc_idx(1, OUT_OF_RESOURCES), unscheduled={"n4": 3}, now=BACKOFF_EPOCH - 100)
    assert list(b) == ["n4"]


def test_generic_capacity_constrained_is_ignored():
    assert blocks(cc_idx(0, CAPACITY_CONSTRAINED)) == {}
    assert blocks(cc_idx(0, CAPACITY_CONSTRAINED, IN_PROGRESS), oldest={"c4": T0 - 999}) == {}


def test_suspended_handled_like_out_of_resources():
    b = blocks(cc_idx(0, SUSPENDED))
    assert b == {"c4": (T0 + 300, "backoff")}  # no timestamp: cooldown
    with_ts = dict(SUSPENDED, message=f"In backoff until {BACKOFF_UNTIL}")
    assert blocks(cc_idx(0, with_ts), now=BACKOFF_EPOCH - 10)["c4"][0] == BACKOFF_EPOCH


def test_unparseable_timestamp_falls_back_to_cooldown():
    bad = dict(OUT_OF_RESOURCES, message="failed. In backoff until sometime soon")
    assert blocks(cc_idx(0, bad)) == {"c4": (T0 + 300, "backoff")}


def test_quota_full_blocks_only_while_full():
    b = blocks(quota_full={"c4"})
    assert b["c4"][1] == "quota full"
    # Spilled: nothing unscheduled, still full -> stays blocked.
    b2 = blocks(quota_full={"c4"}, unscheduled={}, prior=b, now=T0 + 10)
    assert "c4" in b2
    # Quota freed: re-evaluated, lifted immediately (no timer).
    assert blocks(quota_full=(), unscheduled={}, prior=b, now=T0 + 10) == {}
    assert blocks(quota_full={"c4"}, unscheduled={}) == {}  # never had workers waiting


def test_timer_fallback():
    assert blocks(oldest={"c4": T0 - 100}) == {}
    b = blocks(oldest={"c4": T0 - 130})
    assert b == {"c4": (T0 + 300, "timer")}
    # Holds for the cooldown even after workers moved away, then lifts.
    assert "c4" in blocks(unscheduled={}, prior=b, now=T0 + 299)
    assert blocks(unscheduled={}, prior=b, now=T0 + 301) == {}


def _quota(name, limit, used):
    return {
        "metadata": {"name": name},
        "spec": {"limits": {"resources": {"cpu": limit}}},
        "status": {"used": {"resources": {"cpu": used}}},
    }


def test_quota_full_families_uses_one_node_of_vcpus():
    vcpus = {"c4": 8, "n4": 8, "c3": 8}
    quotas = [
        _quota("c4-tier-cap", 96, "90"),  # 6 free < 8
        _quota("n4-tier-cap", 96, "88"),  # 8 free fits one node
        _quota("c3-tier-cap", "24", "24"),
    ]
    assert quota_full_families(quotas, vcpus) == {"c4", "c3"}
    quotas.append(_quota("agent-substrate-class-cap", 720, "716"))
    assert quota_full_families(quotas, vcpus) == {"c4", "n4", "c3"}


def test_family_node_vcpus_from_machine_type():
    cc_obj = {"spec": {"priorities": [{"machineType": "c4-standard-4"}, {"machineType": "n4-standard-8"}]}}
    assert family_node_vcpus(cc_obj, FAMS) == {"c4": 4, "n4": 8, "c3": 8}


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
