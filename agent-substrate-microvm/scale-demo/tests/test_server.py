"""Unit and API integration tests for ccc-agents telemetry backend."""

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
# Unit Tests: Telemetry Assembly & Simulation
# ---------------------------------------------------------------------------


def test_generate_mock_cluster_data_zero():
    data = generate_mock_cluster_data(
        target_agents=0,
        actors_per_worker=2,
        quotas={"n4-tier-cap": 96, "c4-tier-cap": 96, "c2-tier-cap": 96},
    )
    assert data["summary"]["targetAgents"] == 0
    assert data["summary"]["targetMicrovms"] == 0
    assert data["summary"]["totalNodes"] == 0
    assert data["buffer"]["activeReplicas"] == 2
    assert data["buffer"]["standbyReplicas"] == 6


def test_generate_mock_cluster_data_cascade():
    data = generate_mock_cluster_data(
        target_agents=2000,
        actors_per_worker=2,
        quotas={"n4-tier-cap": 96, "c4-tier-cap": 96, "c2-tier-cap": 96},
    )

    summary = data["summary"]
    assert summary["targetAgents"] == 2000
    assert summary["targetMicrovms"] == 1000
    assert summary["totalNodes"] == 40

    nfam = summary["nodesByFamily"]
    assert nfam["c3"] == 3
    assert nfam["n4"] == 12
    assert nfam["c4"] == 12
    assert nfam["c2"] == 12
    assert nfam["n2"] == 1

    # Rule 0 share: 3 / 40 = 7.5%
    assert summary["rule0Share"] == 7.5
    assert summary["fallbackShare"] == 92.5


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


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
        "actorsPerWorker": 2,
        "activeBufferReplicas": 2,
        "standbyReplicas": 6,
    }
    response = client.post("/api/scale", json=req_body)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "success"
    assert res_data["targetAgents"] == 600
    assert res_data["targetMicrovms"] == 300
    assert res_data["activeBufferReplicas"] == 2
    assert res_data["standbyReplicas"] == 6

    # Verify state updated
    assert demo_state["targetAgents"] == 600
    assert demo_state["actorsPerWorker"] == 2
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
                "actorsPerWorker": 2,
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
    # Out of bounds (>2000)
    res1 = client.post("/api/scale", json={"targetAgents": 2500, "actorsPerWorker": 2})
    assert res1.status_code == 422

    # Negative
    res2 = client.post("/api/scale", json={"targetAgents": -10, "actorsPerWorker": 2})
    assert res2.status_code == 422


def test_api_quotas_endpoint():
    req_body = {
        "quotaName": "n4-tier-cap",
        "cpuLimit": 128,
    }
    response = client.post("/api/quotas", json=req_body)
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "success"
    assert demo_state["quotas"]["n4-tier-cap"] == 128

    # Reset
    client.post("/api/quotas", json={"quotaName": "n4-tier-cap", "cpuLimit": 96})


def test_api_quotas_invalid():
    res = client.post("/api/quotas", json={"quotaName": "non-existent-quota", "cpuLimit": 100})
    assert res.status_code == 400


def test_index_html_serving():
    response = client.get("/")
    assert response.status_code == 200
    assert "GKE Agent Substrate - scale performance control tower" in response.text


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
        {"metadata": {"name": f"pod-{i}"}, "status": {"phase": "Running"}}
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
        actors_per_worker=2,
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
    assert telemetry["summary"]["activeAgents"] == 20
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
        actors_per_worker=2,
        now=now,
    )
    p0_gce = telemetry_gce["computeClass"]["priorities"][0]
    assert p0_gce["badgeLabel"] == "SUSPENDED · GCE QUOTA"
    assert p0_gce["quotaInfo"]["scope"] == "gce_project"
    assert p0_gce["quotaInfo"]["limitCpu"] == 16
    assert p0_gce["quotaInfo"]["name"] == "C3D_CPUS"


def test_dual_sandbox_runtime_switching():
    # 1. Switch to gvisor runtime
    res = client.post(
        "/api/scale",
        json={
            "targetAgents": 400,
            "actorsPerWorker": 2,
            "sandboxRuntime": "gvisor",
        },
    )
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["sandboxRuntime"] == "gvisor"
    assert res_data["sandboxRuntimeLabel"] == "gVisor (runsc)"
    assert res_data["targetSandboxes"] == 200
    assert res_data["targetMicrovms"] == 200

    state_res = client.get("/api/state")
    assert state_res.status_code == 200
    current = state_res.json()["current"]
    summary = current["summary"]
    assert summary["sandboxRuntime"] == "gvisor"
    assert summary["sandboxRuntimeLabel"] == "gVisor (runsc)"
    assert summary["computeClassName"] == "agent-substrate-gvisor"
    assert summary["targetSandboxes"] == 200
    assert summary["targetMicrovms"] == 200

    # 2. Switch back to microvm runtime
    res_vm = client.post(
        "/api/scale",
        json={
            "targetAgents": 600,
            "actorsPerWorker": 2,
            "sandboxRuntime": "microvm",
        },
    )
    assert res_vm.status_code == 200
    vm_data = res_vm.json()
    assert vm_data["sandboxRuntime"] == "microvm"
    assert vm_data["sandboxRuntimeLabel"] == "microVM (Kata)"
    assert vm_data["targetSandboxes"] == 300
    assert vm_data["targetMicrovms"] == 300

    state_vm = client.get("/api/state").json()["current"]["summary"]
    assert state_vm["sandboxRuntime"] == "microvm"
    assert state_vm["sandboxRuntimeLabel"] == "microVM (Kata)"
    assert state_vm["computeClassName"] == "agent-substrate-microvm"
    assert state_vm["targetSandboxes"] == 300

    # 3. Verify index.html contains runtime selector and unified sandbox labels
    index_res = client.get("/")
    assert index_res.status_code == 200
    html = index_res.text
    assert 'name="runtime"' in html
    assert 'value="microvm"' in html
    assert 'value="gvisor"' in html
    assert 'id="runtime-badge-val"' in html
    assert 'Actor:Sandbox density:' in html
    assert 'Agents &amp; sandbox worker pods' in html
    assert 'Target sandboxes:' in html
    assert 'Active agents &amp; sandbox pod count over time' in html
    assert 'Average pod scheduling &amp; sandbox creation latency over time' in html

