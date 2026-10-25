"""Unit and API integration tests for the Agent Sandbox control tower backend."""

import os
import sys
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient

# Ensure app is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.server import (
    app,
    demo_state,
    extract_machine_family,
    is_node_suspended,
    extract_priority_index,
    parse_backoff_timestamp,
    parse_compute_class_status,
    compute_pod_latencies,
    aggregate_capacity_buffers,
    assemble_telemetry,
    generate_mock_cluster_data,
    plan_indexed,
    summarize_sandbox_pods,
    compute_claim_latencies,
    classify_sessions,
    record_resume_latencies,
    session_sandbox_body,
    claim_body,
    parse_session_state,
    compare_session_state,
    summarize_survival,
)

# Force mock_mode during unit/API tests for deterministic verification
demo_state["mock_mode"] = True
client = TestClient(app)


# ---------------------------------------------------------------------------
# Unit Tests: Helper Parsing Functions
# ---------------------------------------------------------------------------


def test_extract_machine_family():
    # Label priority
    n1 = {"metadata": {"labels": {"cloud.google.com/machine-family": "n4"}}}
    assert extract_machine_family(n1) == "n4"

    # Instance-type fallback
    n2 = {"metadata": {"labels": {"node.kubernetes.io/instance-type": "c3-standard-8"}}}
    assert extract_machine_family(n2) == "c3"

    n3 = {"metadata": {"labels": {"node.kubernetes.io/instance-type": "c4-standard-16"}}}
    assert extract_machine_family(n3) == "c4"

    n4 = {"metadata": {"labels": {"node.kubernetes.io/instance-type": "c2-standard-4"}}}
    assert extract_machine_family(n4) == "c2"

    n5 = {"metadata": {"labels": {"node.kubernetes.io/instance-type": "n2-standard-32"}}}
    assert extract_machine_family(n5) == "n2"

    # Unknown
    n6 = {"metadata": {"labels": {"node.kubernetes.io/instance-type": "custom-8-32768"}}}
    assert extract_machine_family(n6) == "unknown"


def test_is_node_suspended():
    n_suspended_cond = {
        "status": {"conditions": [{"type": "Suspended", "status": "True"}]}
    }
    assert is_node_suspended(n_suspended_cond) is True

    n_suspended_taint = {
        "spec": {
            "taints": [
                {
                    "key": "buffer.gke.io/standby-capacity-node-suspended",
                    "effect": "NoSchedule",
                }
            ]
        }
    }
    assert is_node_suspended(n_suspended_taint) is True

    n_active = {
        "status": {"conditions": [{"type": "Ready", "status": "True"}]}
    }
    assert is_node_suspended(n_active) is False


def test_extract_priority_index_annotated():
    node = {
        "metadata": {
            "annotations": {"cloud.google.com/ccc_priority_index": "2"},
            "creationTimestamp": "2026-10-23T20:00:00Z",
        }
    }
    assert extract_priority_index(node) == "2"


def test_extract_priority_index_pending_annotation():
    now = datetime(2026, 10, 23, 20, 1, 0, tzinfo=timezone.utc)
    # Created 45 seconds ago (age < 90s)
    created = (now - timedelta(seconds=45)).isoformat()
    node = {
        "metadata": {
            "creationTimestamp": created,
        }
    }
    assert extract_priority_index(node, now=now) == "pending_annotation"


def test_extract_priority_index_unannotated_aged():
    now = datetime(2026, 10, 23, 20, 5, 0, tzinfo=timezone.utc)
    # Created 180 seconds ago (age > 90s)
    created = (now - timedelta(seconds=180)).isoformat()
    node = {
        "metadata": {
            "creationTimestamp": created,
        }
    }
    assert extract_priority_index(node, now=now) == "unannotated"


def test_parse_backoff_timestamp():
    now = datetime(2026, 10, 23, 20, 0, 0, tzinfo=timezone.utc)
    msg = (
        "NodeProvisioning associated with this priority failed due to the "
        "OutOfResources error. In backoff until 2026-10-23 20:05:00 UTC."
    )
    res = parse_backoff_timestamp(msg, now=now)
    assert res["untilUtc"] == "2026-10-23 20:05:00 UTC"
    assert res["remainingSeconds"] == 300


def test_parse_backoff_timestamp_none():
    res = parse_backoff_timestamp("No backoff present")
    assert res["untilUtc"] is None
    assert res["remainingSeconds"] == 0


def test_parse_compute_class_status():
    now = datetime(2026, 10, 23, 20, 0, 0, tzinfo=timezone.utc)
    cc_obj = {
        "status": {
            "conditions": [
                {"type": "Health", "status": "True", "message": "Crd is healthy."}
            ],
            "priorityStatuses": [
                {
                    "identifier": "0",
                    "conditions": [
                        {
                            "type": "ProvisioningSuspended",
                            "message": "Failed until 2026-10-23 20:02:00 UTC.",
                        }
                    ],
                    "resourceInfo": [
                        {
                            "name": "cpu",
                            "currentCount": 24,
                            "targetCount": 24,
                            "currentUtilizationPercentage": 100.0,
                        }
                    ],
                    "scalingEventsHistory": {
                        "provisionedNodesCount": 3,
                        "consolidatedNodesCount": 1,
                        "migratedNodesCount": 0,
                    },
                },
                {
                    "identifier": "1",
                    "conditions": [
                        {
                            "type": "NodeProvisioningInProgress",
                            "message": "2 new nodes will be added with config: {NodePool: nap-n4-pool, MachineType: n4-standard-8, Zones: [us-central1-a]}",
                        }
                    ],
                    "resourceInfo": [
                        {
                            "name": "cpu",
                            "currentCount": 32,
                            "targetCount": 64,
                            "currentUtilizationPercentage": 50.0,
                        }
                    ],
                },
            ],
        }
    }

    parsed = parse_compute_class_status(cc_obj, now=now)
    assert parsed["topLevel"]["healthy"] is True
    assert len(parsed["priorities"]) == 2

    p0 = parsed["priorities"][0]
    assert p0["identifier"] == "0"
    assert p0["state"] == "ProvisioningSuspended"
    assert p0["backoff"]["remainingSeconds"] == 120
    assert p0["cpu"]["current"] == 24
    assert p0["scalingEvents"]["provisioned"] == 3

    p1 = parsed["priorities"][1]
    assert p1["identifier"] == "1"
    assert p1["state"] == "NodeProvisioningInProgress"
    assert "nap-n4-pool" in p1["inProgress"]
    assert p1["inProgressNodes"] == 2
    assert p1["cpu"]["target"] == 64


def test_aggregate_capacity_buffers_hybrid():
    buffers = [
        {
            "metadata": {
                "name": "agent-active-buffer",
                "labels": {"buffer-strategy": "active"},
            },
            "spec": {
                "replicas": 2,
                "provisioningStrategy": "buffer.x-k8s.io/active-capacity",
            },
            "status": {
                "conditions": [
                    {"type": "ReadyForProvisioning", "status": "True"},
                    {"type": "Provisioning", "status": "True"},
                ]
            },
        },
        {
            "metadata": {
                "name": "agent-standby-buffer-n4",
                "labels": {"machine-family": "n4"},
            },
            "spec": {
                "replicas": 1,
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {
                "conditions": [
                    {"type": "ReadyForProvisioning", "status": "True"},
                ]
            },
        },
        {
            "metadata": {
                "name": "agent-standby-buffer-n2",
                "labels": {"machine-family": "n2"},
            },
            "spec": {
                "replicas": 2,
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {
                "conditions": [
                    {"type": "ReadyForProvisioning", "status": "True"},
                ]
            },
        },
    ]
    res = aggregate_capacity_buffers(buffers=buffers, suspended_nodes=2)
    assert res["activeReplicas"] == 2
    assert res["standbyReplicas"] == 3
    assert res["standbyByFamily"]["n4"] == 1
    assert res["standbyByFamily"]["n2"] == 2
    assert res["suspendedNodes"] == 2
    assert res["readyBuffers"] == 3
    assert res["provisioningBuffers"] == 1


def test_compute_pod_latencies():
    t0 = datetime(2026, 10, 23, 20, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=1.5)  # Sched latency 1.5s
    t2 = t1 + timedelta(seconds=3.5)  # Boot latency 3.5s

    pods = [
        {
            "metadata": {"creationTimestamp": t0.isoformat()},
            "status": {
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "True",
                        "lastTransitionTime": t1.isoformat(),
                    },
                    {
                        "type": "ContainersReady",
                        "status": "True",
                        "lastTransitionTime": t2.isoformat(),
                    },
                ]
            },
        }
    ]

    lat = compute_pod_latencies(pods)
    assert lat["sampleCount"] == 1
    assert lat["scheduling"]["avg"] == 1.5
    assert lat["microvmBoot"]["avg"] == 3.5


# ---------------------------------------------------------------------------
# Unit Tests: Agent Sandbox helpers
# ---------------------------------------------------------------------------


def _ts(sec):
    return datetime.fromtimestamp(sec, timezone.utc).isoformat()


def _pod(name, ready, created=1000, labels=None, ready_at=None):
    conds = []
    if ready:
        conds = [{"type": "Ready", "status": "True", "lastTransitionTime": _ts(ready_at or created + 5)}]
    return {
        "metadata": {"name": name, "creationTimestamp": _ts(created), "labels": labels or {}},
        "status": {"phase": "Running" if ready else "Pending", "conditions": conds},
    }


def test_plan_indexed_fills_gaps_and_trims_from_the_top():
    create, delete = plan_indexed(["agent-gvisor-0000", "agent-gvisor-0002"], "agent-gvisor-", 4)
    assert create == ["agent-gvisor-0001", "agent-gvisor-0003"]
    assert delete == []
    create, delete = plan_indexed(
        ["agent-gvisor-0000", "agent-gvisor-0001", "agent-gvisor-0002"], "agent-gvisor-", 1
    )
    assert create == []
    assert delete == ["agent-gvisor-0002", "agent-gvisor-0001"]
    create, _ = plan_indexed([], "session-", 500, max_creates=100)
    assert len(create) == 100 and create[0] == "session-0000"


def test_summarize_sandbox_pods_splits_warm_and_claimed():
    warm = {"agents.x-k8s.io/warm-pool-sandbox": "abc"}
    pods = [_pod("w1", True, labels=warm), _pod("w2", False, labels=warm),
            _pod("c1", True), _pod("c2", True), _pod("c3", False)]
    assert summarize_sandbox_pods(pods) == {
        "warmReady": 1, "warmPending": 1, "claimedReady": 2, "claimedPending": 1,
    }


def test_compute_claim_latencies_counts_warm_hits():
    claims = [
        # Adopted a warm pod created before the claim: a warm hit, Ready in 1s.
        {"metadata": {"name": "a", "creationTimestamp": _ts(1000)},
         "status": {"sandbox": {"name": "p1"},
                    "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": _ts(1001)}]}},
        # Cold: its pod was created after the claim, Ready in 45s.
        {"metadata": {"name": "b", "creationTimestamp": _ts(1000)},
         "status": {"sandbox": {"name": "p2"},
                    "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": _ts(1045)}]}},
        # Not Ready yet: excluded.
        {"metadata": {"name": "c", "creationTimestamp": _ts(1000)}, "status": {}},
    ]
    pods = [_pod("p1", True, created=900), _pod("p2", True, created=1002)]
    stats = compute_claim_latencies(claims, pods)
    assert stats["count"] == 2
    assert stats["warmHits"] == 1
    assert stats["avg"] == 23.0


def test_classify_sessions_and_resume_latency():
    sandboxes = [
        {"metadata": {"name": "s-run"}, "spec": {"operatingMode": "Running"}},
        {"metadata": {"name": "s-start"}, "spec": {"operatingMode": "Running"}},
        {"metadata": {"name": "s-susp"}, "spec": {"operatingMode": "Suspended"}},
        {"metadata": {"name": "s-snap"}, "spec": {"operatingMode": "Running"}},
        {"metadata": {"name": "s-err"}, "spec": {"operatingMode": "Running"}},
    ]
    pods = [_pod("s-run", True), _pod("s-start", False), _pod("s-snap", True), _pod("s-err", True)]
    ops = {"s-snap": {"state": "snapshotting"}, "s-err": {"state": "error", "error": "x"}}
    out = classify_sessions(sandboxes, pods, ops)
    assert out["counts"] == {
        "running": 1, "starting": 1, "snapshotting": 1, "suspended": 1, "error": 1, "total": 5,
    }

    started = {"s-run": 1000.0, "s-start": 1000.0}
    sink = []
    record_resume_latencies([_pod("s-run", True, created=1001, ready_at=1012), _pod("s-start", False)],
                            started, sink)
    assert sink == [12.0]
    assert "s-run" not in started and "s-start" in started


def test_session_body_labels_the_pod_for_snapshot_grouping():
    template = {"spec": {"podTemplate": {"metadata": {"labels": {"app": "agent-session"}},
                                         "spec": {"runtimeClassName": "gvisor"}}}}
    body = session_sandbox_body("session-0007", template)
    assert body["kind"] == "Sandbox"
    assert body["metadata"]["labels"]["sbx-demo/role"] == "session"
    assert body["spec"]["podTemplate"]["metadata"]["labels"] == {
        "app": "agent-session", "session": "session-0007",
    }
    # The template itself is not mutated.
    assert "session" not in template["spec"]["podTemplate"]["metadata"]["labels"]

    claim = claim_body("agent-microvm-0001", "microvm")
    assert claim["spec"] == {"warmPoolRef": {"name": "agent-microvm-warmpool"}}
    assert claim["metadata"]["labels"]["sbx-demo/runtime"] == "microvm"


# ---------------------------------------------------------------------------
# Unit Tests: Telemetry Assembly & Simulation
# ---------------------------------------------------------------------------

QUOTAS = {
    "sbx-demo-class-cap": 720,
    "sbx-demo-c3-cap": 24,
    "sbx-demo-n4-cap": 96,
    "sbx-demo-c4-cap": 96,
    "sbx-demo-c2-cap": 96,
}


def test_generate_mock_cluster_data_zero():
    data = generate_mock_cluster_data(
        target_agents=0, quotas=QUOTAS, sandbox_runtime="microvm", warm_pool_replicas=0
    )
    assert data["summary"]["mode"] == "stateless"
    assert data["summary"]["targetAgents"] == 0
    assert data["summary"]["activeAgents"] == 0
    assert data["summary"]["totalNodes"] == 0
    assert data["buffer"]["activeReplicas"] == 2
    assert data["buffer"]["standbyReplicas"] == 6


def test_generate_mock_cluster_data_cascade():
    # microVM packs 14 sandboxes per 8-vCPU node in the simulator.
    data = generate_mock_cluster_data(
        target_agents=600, quotas=QUOTAS, sandbox_runtime="microvm", warm_pool_replicas=10
    )
    summary = data["summary"]
    assert summary["targetAgents"] == 600
    assert summary["activeAgents"] == 600
    assert summary["warmPool"] == {
        "name": "agent-microvm-warmpool", "target": 10, "ready": 10, "pending": 0,
    }
    nfam = summary["nodesByFamily"]
    assert nfam["c3"] == 3  # capped by sbx-demo-c3-cap (24 vCPU)
    assert nfam["n4"] == 12
    assert nfam["c4"] == 12
    assert nfam["c2"] == 12
    assert nfam["n2"] == 5
    assert summary["totalNodes"] == 44
    assert data["quotas"]["sbx-demo-c3-cap"]["capped"] is True
    assert data["latencies"]["claimReady"]["warmHits"] == 10


def test_api_state_endpoint():
    response = client.get("/api/state")
    assert response.status_code == 200
    json_data = response.json()
    assert "current" in json_data
    assert "history" in json_data
    assert "summary" in json_data["current"]


def test_api_scale_endpoint():
    req_body = {
        "targetAgents": 600,
        "warmPoolReplicas": 20,
        "activeBufferReplicas": 2,
        "standbyReplicas": 6,
    }
    response = client.post("/api/scale", json=req_body)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "success"
    assert res_data["targetAgents"] == 600
    assert res_data["targetSandboxes"] == 600
    assert res_data["warmPoolReplicas"] == 20
    assert res_data["activeBufferReplicas"] == 2
    assert res_data["standbyReplicas"] == 6

    assert demo_state["targetAgents"] == 600
    assert demo_state["warmPoolReplicas"] == 20
    assert demo_state["standbyReplicas"] == 6
    assert res_data["standbyByFamily"] == {"c3": 1, "n4": 1, "c4": 1, "c2": 1, "n2": 2}


def test_api_scale_buffer_replicas_update():
    from app.server import distribute_standby_replicas

    assert distribute_standby_replicas(0) == {"c3": 0, "n4": 0, "c4": 0, "c2": 0, "n2": 0}
    assert distribute_standby_replicas(6) == {"c3": 1, "n4": 1, "c4": 1, "c2": 1, "n2": 2}
    assert distribute_standby_replicas(12) == {"c3": 2, "n4": 3, "c4": 2, "c2": 2, "n2": 3}

    prev_mock = demo_state["mock_mode"]
    demo_state["mock_mode"] = True
    try:
        res = client.post(
            "/api/scale",
            json={
                "targetAgents": 0,
                "activeBufferReplicas": 4,
                "standbyReplicas": 12,
                "consolidationDelayMinutes": 2,
            },
        )
        assert res.status_code == 200
        assert res.json()["consolidationDelayMinutes"] == 2
        state_res = client.get("/api/state").json()["current"]
        assert state_res["buffer"]["activeReplicas"] == 4
        assert state_res["buffer"]["standbyReplicas"] == 12
        assert state_res["summary"]["consolidationDelayMinutes"] == 2
        assert state_res["buffer"]["standbyByFamily"] == {
            "c3": 2,
            "n4": 3,
            "c4": 2,
            "c2": 2,
            "n2": 3,
        }
        reset_res = client.post("/api/reset")
        assert reset_res.status_code == 200
        assert reset_res.json()["status"] == "success"
        after_reset = client.get("/api/state").json()
        assert after_reset["resetEpoch"] >= 1
    finally:
        demo_state["mock_mode"] = prev_mock


def test_api_scale_validation_bounds():
    assert client.post("/api/scale", json={"targetAgents": 2500}).status_code == 422
    assert client.post("/api/scale", json={"targetAgents": -10}).status_code == 422
    assert client.post("/api/scale", json={"targetAgents": 10, "warmPoolReplicas": 500}).status_code == 422


def test_api_quotas_endpoint():
    response = client.post("/api/quotas", json={"quotaName": "sbx-demo-n4-cap", "cpuLimit": 128})
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert demo_state["quotas"]["sbx-demo-n4-cap"] == 128
    client.post("/api/quotas", json={"quotaName": "sbx-demo-n4-cap", "cpuLimit": 96})


def test_api_quotas_invalid():
    res = client.post("/api/quotas", json={"quotaName": "non-existent-quota", "cpuLimit": 100})
    assert res.status_code == 400


def test_api_sessions_needs_snapshots_mode():
    res = client.post("/api/sessions", json={"action": "suspend"})
    assert res.status_code == 400


def test_index_html_serving():
    response = client.get("/")
    assert response.status_code == 200
    assert "GKE Agent Sandbox - scale performance control tower" in response.text

def test_dynamic_machine_families_and_status_disambiguation():
    from app.server import extract_priority_spec_rules, distribute_standby_replicas

    # 1. Dynamic GCE families outside the default 5 (e.g. c3d, n4d, t2d)
    assert extract_machine_family(
        {"metadata": {"labels": {"node.kubernetes.io/instance-type": "c3d-standard-8"}}}
    ) == "c3d"
    assert extract_machine_family(
        {"metadata": {"labels": {"node.kubernetes.io/instance-type": "n4d-standard-16"}}}
    ) == "n4d"

    # 2. Custom ComputeClass with 3 non-default families (c3d -> n4d -> t2d)
    now = datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone.utc)
    custom_cc = {
        "spec": {
            "priorities": [
                {"machineType": "c3d-standard-8"},
                {"machineType": "n4d-standard-8"},
                {"machineFamily": "t2d", "minCores": 8},
            ]
        },
        "status": {
            "conditions": [{"type": "Health", "status": "True", "message": "OK"}],
            "priorityStatuses": [
                {
                    "identifier": "0",
                    "conditions": [
                        {
                            "type": "ProvisioningSuspended",
                            "reason": "OutOfResources",
                            "message": "NodeProvisioning failed due to OutOfResources. In backoff until 2026-10-24 00:05:00 UTC.",
                        }
                    ],
                },
                {
                    "identifier": "1",
                    "conditions": [
                        {
                            "type": "ProvisioningConstrained",
                            "reason": "OutOfResources",
                            "message": "NodeProvisioning failed due to OutOfResources. In backoff until 2026-10-24 00:04:00 UTC.",
                        },
                        {
                            "type": "ProvisioningConstrained",
                            "reason": "CapacityConstrained",
                            "message": "A subset (3/6) of configurations are excluded due to active capacity cooldowns.",
                        },
                    ],
                },
                {
                    "identifier": "2",
                    "conditions": [],
                },
                {
                    "identifier": "ScaleUpAnyway",
                    "conditions": [],
                },
            ],
        },
    }

    rules = extract_priority_spec_rules(custom_cc)
    assert [r["family"] for r in rules] == ["c3d", "n4d", "t2d"]
    assert distribute_standby_replicas(5, ["c3d", "n4d", "t2d"]) == {
        "c3d": 1,
        "n4d": 2,
        "t2d": 2,
    }

    nodes = [
        {
            "metadata": {
                "labels": {"cloud.google.com/machine-family": "n4d"},
                "annotations": {"cloud.google.com/ccc_priority_index": "1"},
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for _ in range(4)
    ]
    # Add 2 suspended standby nodes (1 c3d, 1 t2d) — must NOT be counted in active nodesByFamily or totalNodes
    nodes.extend(
        [
            {
                "metadata": {
                    "labels": {"cloud.google.com/machine-family": "c3d"},
                    "annotations": {"cloud.google.com/ccc_priority_index": "0"},
                },
                "spec": {
                    "taints": [
                        {
                            "key": "buffer.gke.io/standby-capacity-node-suspended",
                            "effect": "NoSchedule",
                        }
                    ]
                },
                "status": {"conditions": [{"type": "Suspended", "status": "True"}]},
            },
            {
                "metadata": {
                    "labels": {"cloud.google.com/machine-family": "t2d"},
                    "annotations": {"cloud.google.com/ccc_priority_index": "2"},
                },
                "status": {"conditions": [{"type": "Suspended", "status": "True"}]},
            },
        ]
    )
    pods = [
        {
            "metadata": {"name": f"pod-{i}"},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }
        for i in range(10)
    ]
    quotas = [
        {
            "metadata": {"name": "n4d-cap"},
            "spec": {
                "selector": {
                    "matchLabels": {"cloud.google.com/machine-family": "n4d"}
                },
                "limits": {"resources": {"cpu": 32}},
            },
        },
        {
            "metadata": {"name": "custom-class-cap"},
            "spec": {
                "selector": {
                    "matchLabels": {
                        "cloud.google.com/compute-class": "agent-substrate-microvm"
                    }
                },
                "limits": {"resources": {"cpu": 240}},
            },
        },
    ]

    telemetry = assemble_telemetry(
        nodes=nodes,
        pods=pods,
        cc_obj=custom_cc,
        quotas=quotas,
        target_agents=100,
        now=now,
    )
    assert telemetry["summary"]["familyOrder"] == ["c3d", "n4d", "t2d"]
    assert telemetry["summary"]["totalNodes"] == 4
    assert telemetry["summary"]["suspendedNodes"] == 2
    assert telemetry["summary"]["nodesByFamily"]["c3d"] == 0
    assert telemetry["summary"]["nodesByFamily"]["n4d"] == 4
    assert telemetry["summary"]["nodesByFamily"]["t2d"] == 0
    assert telemetry["summary"]["suspendedByFamily"]["c3d"] == 1
    assert telemetry["summary"]["suspendedByFamily"]["t2d"] == 1
    assert telemetry["summary"]["activeAgents"] == 10  # one agent per claimed sandbox
    priorities = telemetry["computeClass"]["priorities"]
    assert len(priorities) == 3

    # Tier 0 (c3d): Suspended due to OutOfResources (Stockout) + class-wide cap fallback (no hardcoded GCE 24-vCPU assumption)
    assert priorities[0]["badgeLabel"] == "SUSPENDED · STOCKOUT"
    assert priorities[0]["backoff"]["remainingSeconds"] == 300
    assert priorities[0]["quotaInfo"]["scope"] == "class"
    assert priorities[0]["quotaInfo"]["limitCpu"] == 240

    # Tier 1 (n4d): 4 nodes * 8c = 32c == 32c limit -> CAPPED · CAPACITYQUOTA + CONSTRAINED · STOCKOUT (3/6 configs) + preserved 240s backoff timer!
    assert priorities[1]["isQuotaCapped"] is True
    assert priorities[1]["badgeLabel"] == "CAPPED · CAPACITYQUOTA"
    assert priorities[1]["secondaryBadge"] == "CONSTRAINED · STOCKOUT (3/6 configs)"
    assert priorities[1]["backoff"]["remainingSeconds"] == 240

    # Tier 2 (t2d): Active with class-wide quota bar
    assert priorities[2]["badgeLabel"] == "Active"
    assert priorities[2]["quotaInfo"]["scope"] == "class"
    assert priorities[2]["quotaInfo"]["limitCpu"] == 240

    # Verify dynamic GCE quota inference when Cluster Autoscaler reports QuotaExceeded
    custom_cc["status"]["priorityStatuses"][0]["conditions"] = [
        {
            "type": "ProvisioningSuspended",
            "reason": "QuotaExceeded",
            "message": "Instance creation failed: Quota 'C3D_CPUS' exceeded. Limit: 16.0 in region us-central1. In backoff until 2026-10-24 00:05:00 UTC.",
        }
    ]
    telemetry_gce = assemble_telemetry(
        nodes=nodes,
        pods=pods,
        cc_obj=custom_cc,
        quotas=quotas,
        target_agents=100,
        now=now,
    )
    p0_gce = telemetry_gce["computeClass"]["priorities"][0]
    assert p0_gce["badgeLabel"] == "SUSPENDED · GCE QUOTA"
    assert p0_gce["quotaInfo"]["scope"] == "gce_project"
    assert p0_gce["quotaInfo"]["limitCpu"] == 16
    assert p0_gce["quotaInfo"]["name"] == "C3D_CPUS"



def test_dual_sandbox_runtime_switching():
    res = client.post("/api/scale", json={"targetAgents": 400, "sandboxRuntime": "gvisor"})
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["sandboxRuntime"] == "gvisor"
    assert res_data["sandboxRuntimeLabel"] == "gVisor (runsc)"
    assert res_data["targetSandboxes"] == 400

    summary = client.get("/api/state").json()["current"]["summary"]
    assert summary["sandboxRuntime"] == "gvisor"
    assert summary["computeClassName"] == "agent-sandbox-demo-gvisor"
    assert summary["warmPool"]["name"] == "agent-gvisor-warmpool"

    res_vm = client.post("/api/scale", json={"targetAgents": 600, "sandboxRuntime": "microvm"})
    assert res_vm.status_code == 200
    assert res_vm.json()["sandboxRuntimeLabel"] == "microVM (Kata)"
    state_vm = client.get("/api/state").json()["current"]["summary"]
    assert state_vm["computeClassName"] == "agent-sandbox-demo-microvm"
    assert state_vm["targetSandboxes"] == 600

    html = client.get("/").text
    assert 'name="runtime"' in html
    assert 'value="microvm"' in html
    assert 'value="gvisor"' in html
    assert 'id="runtime-badge-val"' in html
    assert 'id="warm-pool-input"' in html
    assert 'id="session-group"' in html
    assert 'Target sandboxes:' in html


SNAPSHOTS_SCRIPT = r"""
import sys
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from app.server import app, demo_state, generate_mock_cluster_data

demo_state["mock_mode"] = True
assert demo_state["quotas"] == {"sbx-demo-snapshots-cap": 192}

# One family, size fallback: n4-standard-8 stocks out at 6 nodes in the simulator, the
# rest lands on n4-standard-16 until the 192-vCPU class cap.
data = generate_mock_cluster_data(400, demo_state["quotas"])
s = data["summary"]
assert s["mode"] == "snapshots" and s["sandboxRuntime"] == "gvisor", s
assert s["familyOrder"] == ["n4-standard-8", "n4-standard-16", "n4-standard-4"], s["familyOrder"]
assert s["nodesByFamily"]["n4-standard-8"] == 6
assert s["totalCpu"] == 192
assert data["quotas"]["sbx-demo-snapshots-cap"]["capped"] is True
assert s["sessions"]["running"] + s["sessions"]["starting"] == 400
assert s["warmPool"]["target"] == 0

client = TestClient(app)
r = client.post("/api/scale", json={"targetAgents": 20, "warmPoolReplicas": 50, "sandboxRuntime": "microvm"})
assert r.status_code == 200, r.text
assert r.json()["sandboxRuntime"] == "gvisor"  # the runtime is fixed in this mode
assert r.json()["warmPoolReplicas"] == 0
assert r.json()["standbyByFamily"] == {"n4": 2}

assert client.post("/api/sessions", json={"action": "suspend"}).status_code == 200
s = client.get("/api/state").json()["current"]["summary"]
assert s["sessions"]["suspended"] == 20 and s["totalNodes"] == 0, s
assert client.post("/api/sessions", json={"action": "resume"}).status_code == 200
s = client.get("/api/state").json()["current"]["summary"]
assert s["sessions"]["running"] == 20, s
assert client.post("/api/sessions", json={"action": "pause"}).status_code == 422
print("ok")
"""


def test_snapshots_mode():
    """DEMO_MODE is read at import, so snapshots mode runs in its own interpreter."""
    import subprocess

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out = subprocess.run(
        [sys.executable, "-c", SNAPSHOTS_SCRIPT, root],
        env={**os.environ, "DEMO_MODE": "snapshots"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("ok")


def test_capacity_warmup():
    from app.server import capacity_warmup
    warm = {"totalBuffers": 2, "readyBuffers": 2, "standbyReplicas": 6, "suspendedNodes": 6}
    assert capacity_warmup(warm)["ready"]
    assert not capacity_warmup({**warm, "suspendedNodes": 3})["ready"]
    assert not capacity_warmup({**warm, "readyBuffers": 1})["ready"]
    assert capacity_warmup({}, mock=True)["ready"]


def test_session_state_survival_helpers():
    line = '{"boots": 1, "firstBootAt": 100, "fileWrites": 7, "processStart": 100, "memTicks": 7}'
    before = parse_session_state("noise\n" + line + "\n")
    assert before["fileWrites"] == 7
    assert parse_session_state("") is None
    assert parse_session_state("{not json}\nplain") is None
    assert parse_session_state('{"a": 1}') is None  # not a session state line

    # Rootfs-only restore: same state file, new process.
    after = {"boots": 2, "firstBootAt": 100, "fileWrites": 9, "processStart": 200, "memTicks": 0}
    assert compare_session_state(before, after) == {"fileKept": True, "memoryKept": False}
    # Cold start: the file is gone too.
    cold = {"boots": 1, "firstBootAt": 300, "fileWrites": 0, "processStart": 300, "memTicks": 0}
    assert compare_session_state(before, cold) == {"fileKept": False, "memoryKept": False}
    # The same process continuing (whole-pod) keeps both.
    same = {**before, "fileWrites": 8, "memTicks": 8}
    assert compare_session_state(before, same) == {"fileKept": True, "memoryKept": True}
    # Unknown when either side was never read: no guess.
    assert compare_session_state(None, after) is None
    assert compare_session_state(before, None) is None

    agg = summarize_survival({"a": {"fileKept": True, "memoryKept": False},
                              "b": {"fileKept": False, "memoryKept": False}}, pending=1)
    assert agg == {"checked": 2, "pending": 1, "fileKept": 1, "memoryKept": 0}


ROOTFS_SCRIPT = r"""
import sys
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from app.server import app, demo_state, generate_mock_cluster_data, get_runtime_config

demo_state["mock_mode"] = True
assert get_runtime_config()["label"] == "gVisor + Pod Snapshots (rootfs-only)"
assert get_runtime_config()["groupBy"] == "family"

# Wide multi-family ladder like stateless gVisor: c3 hits its quota, the rest spills over.
data = generate_mock_cluster_data(300, demo_state["quotas"])
s = data["summary"]
assert s["mode"] == "snapshots-rootfs" and s["sandboxRuntime"] == "gvisor", s
assert s["computeClassName"] == "agent-sandbox-demo-gvisor"
assert s["familyOrder"][:3] == ["c3", "n4", "c4"], s["familyOrder"]
assert len([f for f, n in s["nodesByFamily"].items() if n]) > 1, s["nodesByFamily"]
assert s["sessions"]["running"] + s["sessions"]["starting"] == 300
assert s["warmPool"]["target"] == 0
assert s["resumeSurvival"] is None

client = TestClient(app)
r = client.post("/api/scale", json={"targetAgents": 20, "warmPoolReplicas": 50, "sandboxRuntime": "microvm"})
assert r.status_code == 200, r.text
assert r.json()["mode"] == "snapshots-rootfs"
assert r.json()["sandboxRuntime"] == "gvisor" and r.json()["warmPoolReplicas"] == 0
assert set(r.json()["standbyByFamily"]) == {"c3", "n4", "c4", "c2", "n2"}

assert client.post("/api/sessions", json={"action": "suspend"}).status_code == 200
s = client.get("/api/state").json()["current"]["summary"]
assert s["sessions"]["suspended"] == 20 and s["resumeSurvival"] is None, s
assert client.post("/api/sessions", json={"action": "resume"}).status_code == 200
s = client.get("/api/state").json()["current"]["summary"]
assert s["sessions"]["running"] == 20, s
# The mock simulates the documented outcome and says so.
assert s["resumeSurvival"] == {"checked": 20, "pending": 0, "fileKept": 20, "memoryKept": 0, "simulated": True}, s
print("ok")
"""


def test_snapshots_rootfs_mode():
    """DEMO_MODE is read at import, so rootfs-only mode runs in its own interpreter."""
    import subprocess

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out = subprocess.run(
        [sys.executable, "-c", ROOTFS_SCRIPT, root],
        env={**os.environ, "DEMO_MODE": "snapshots-rootfs"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("ok")
