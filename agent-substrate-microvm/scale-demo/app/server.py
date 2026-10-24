#!/usr/bin/env python3
"""FastAPI telemetry backend and surge reconciler for ccc-agents demo."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("ccc-agents")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(telemetry_background_sampler())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="GKE Agent Substrate - scale performance control tower",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory target state
demo_state: Dict[str, Any] = {
    "targetAgents": 0,
    "actorsPerWorker": 2,
    "sandboxRuntime": "microvm",
    "activeBufferReplicas": 2,
    "standbyReplicas": 6,
    "consolidationDelayMinutes": 1,
    "quotas": {
        "agent-substrate-class-cap": 720,
        "c3-tier-cap": 24,
        "n4-tier-cap": 96,
        "c4-tier-cap": 96,
        "c2-tier-cap": 96,
    },
    "history": [],  # Ring buffer of time-series samples (up to 60m)
    "mock_mode": False,
    "started_at": datetime.now(timezone.utc),
    "metrics_reset_at": None,
    "scaling_events_baseline": {},
    "resetEpoch": 0,
}

MAX_HISTORY_LEN = 3600
DEMO_NAMESPACE = os.environ.get("DEMO_NAMESPACE", "agent-substrate-demo")
_k8s_clients: Optional[Tuple[Any, Any, Any]] = None
_cached_cluster_name: Optional[str] = None

# ---------------------------------------------------------------------------
# Helper Parsing Functions (Modular & Unit-Testable)
# ---------------------------------------------------------------------------

DEFAULT_FAMILIES = ["c3", "n4", "c4", "c2", "n2"]


def get_runtime_config(runtime: Optional[str] = None) -> Dict[str, str]:
    """Returns resource names and labels for the active sandbox runtime."""
    rt = (runtime or demo_state.get("sandboxRuntime") or "microvm").strip().lower()
    if rt == "gvisor":
        return {
            "runtime": "gvisor",
            "label": "gVisor (runsc)",
            "computeClass": "agent-substrate-gvisor",
            "deployment": "agent-substrate-gvisor-workers",
            "workerPool": "agent-substrate-gvisor-pool",
            "podLabel": "agent-substrate-gvisor-worker",
            "bufferPrefix": "agent-gvisor-buffer",
        }
    return {
        "runtime": "microvm",
        "label": "microVM (Kata)",
        "computeClass": "agent-substrate-microvm",
        "deployment": "agent-substrate-microvm-workers",
        "workerPool": "agent-substrate-microvm-pool",
        "podLabel": "agent-substrate-microvm-worker",
        "bufferPrefix": "agent-microvm-buffer",
    }
GCE_FAMILY_PREFIX_REGEX = re.compile(r"^[a-z]+\d+[a-z]*$")
EXCLUDED_CONFIGS_REGEX = re.compile(
    r"\((\d+/\d+)\)\s+of\s+configurations", re.IGNORECASE
)
GCE_QUOTA_LIMIT_REGEX = re.compile(
    r"Limit:\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)
CORES_SUFFIX_REGEX = re.compile(r"-(\d+)$")


def extract_machine_family(node: Dict[str, Any]) -> str:
    """Extracts node machineFamily dynamically from labels or instance-type prefix."""
    labels = node.get("metadata", {}).get("labels", {})
    if "cloud.google.com/machine-family" in labels:
        fam = str(labels["cloud.google.com/machine-family"]).strip().lower()
        if fam:
            return fam

    instance_type = (
        labels.get("node.kubernetes.io/instance-type")
        or labels.get("beta.kubernetes.io/instance-type")
        or ""
    )
    if instance_type:
        parts = instance_type.strip().lower().split("-")
        if parts and GCE_FAMILY_PREFIX_REGEX.match(parts[0]):
            return parts[0]

    return "unknown"


def extract_priority_spec_rules(
    cc_obj: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Extracts ordered priority tier definitions dynamically from ComputeClass.spec.priorities."""
    if not cc_obj:
        return []
    spec_priorities = cc_obj.get("spec", {}).get("priorities", [])
    if not isinstance(spec_priorities, list) or not spec_priorities:
        return []

    rules = []
    total_rules = len(spec_priorities)
    for idx, pr in enumerate(spec_priorities):
        if not isinstance(pr, dict):
            continue
        ident = str(idx)
        mtype = pr.get("machineType", "")
        mfam = pr.get("machineFamily", "")
        min_cores = pr.get("minCores")
        is_spot = bool(pr.get("spot", False))

        if mtype:
            fam = (mfam or mtype.split("-")[0]).strip().lower()
            m_cores = CORES_SUFFIX_REGEX.search(mtype)
            cores = int(m_cores.group(1)) if m_cores else int(min_cores or 8)
            desc = mtype + (" · Spot" if is_spot else "")
        elif mfam:
            fam = mfam.strip().lower()
            cores = int(min_cores or 8)
            desc = f"{fam} family floor" + (
                f" (minCores: {min_cores})" if min_cores else ""
            )
        else:
            fam = f"tier{idx}"
            cores = int(min_cores or 8)
            desc = "custom priority rule"

        if idx == 0:
            role_note = "Primary preferred tier (Rule #0)"
        elif idx == total_rules - 1 and mfam and not mtype:
            role_note = "Elastic catch-all floor tier"
        else:
            role_note = f"Priority #{idx} fallback tier"

        rules.append(
            {
                "identifier": ident,
                "family": fam,
                "machineType": mtype or f"{fam}-family",
                "name": f"Tier {idx}: {fam} ({desc})",
                "coresPerNode": cores,
                "spot": is_spot,
                "note": role_note,
            }
        )
    return rules


def is_node_suspended(node: Dict[str, Any]) -> bool:
    """Checks whether a GKE standby CapacityBuffer node is currently in Suspended state."""
    for cond in node.get("status", {}).get("conditions", []):
        if cond.get("type") == "Suspended" and cond.get("status") == "True":
            return True
    for taint in node.get("spec", {}).get("taints", []):
        if taint.get("key") == "buffer.gke.io/standby-capacity-node-suspended":
            return True
    return False


def extract_priority_index(
    node: Dict[str, Any], now: Optional[datetime] = None
) -> str:
    """Extracts ccc_priority_index annotation, handling ~60-90s registration lag."""
    annotations = node.get("metadata", {}).get("annotations", {})
    if "ccc_priority_index" in annotations:
        return str(annotations["ccc_priority_index"])
    if "cloud.google.com/ccc_priority_index" in annotations:
        return str(annotations["cloud.google.com/ccc_priority_index"])

    creation_ts = node.get("metadata", {}).get("creationTimestamp")
    if creation_ts:
        try:
            if isinstance(creation_ts, str):
                created_dt = datetime.fromisoformat(
                    creation_ts.replace("Z", "+00:00")
                )
            else:
                created_dt = creation_ts
            if now is None:
                now = datetime.now(timezone.utc)
            age_seconds = (now - created_dt).total_seconds()
            if age_seconds < 90:
                return "pending_annotation"
        except Exception:
            pass

    return "unannotated"


BACKOFF_REGEX = re.compile(
    r"until\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+UTC", re.IGNORECASE
)
IN_PROGRESS_NODES_REGEX = re.compile(
    r"(\d+)\s+new\s+nodes?\s+will\s+be\s+added", re.IGNORECASE
)


def parse_backoff_timestamp(
    message: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Parses 'until YYYY-MM-DD HH:MM:SS UTC' and computes remaining seconds."""
    match = BACKOFF_REGEX.search(message)
    if not match:
        return {"untilUtc": None, "remainingSeconds": 0}

    utc_str = match.group(1)
    try:
        until_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        if now is None:
            now = datetime.now(timezone.utc)
        diff = (until_dt - now).total_seconds()
        remaining = max(0, int(diff))
        return {"untilUtc": utc_str + " UTC", "remainingSeconds": remaining}
    except Exception:
        return {"untilUtc": utc_str + " UTC", "remainingSeconds": 0}


def parse_compute_class_status(
    cc_obj: Dict[str, Any],
    now: Optional[datetime] = None,
    events_baseline: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """Parses ComputeClass status conditions, priorityStatuses, and metrics."""
    status = cc_obj.get("status", {})
    conditions = status.get("conditions", [])

    consolidation_delay = (
        cc_obj.get("spec", {})
        .get("autoscalingPolicy", {})
        .get("consolidationDelayMinutes", 1)
    )
    top_level_health = {
        "healthy": True,
        "crdMisconfigured": False,
        "unableToProvision": False,
        "consolidationDelayMinutes": int(consolidation_delay),
        "messages": [],
    }

    for cond in conditions:
        ctype = cond.get("type")
        cstatus = cond.get("status")
        cmsg = cond.get("message", "")
        if ctype == "Health" and cstatus == "False":
            top_level_health["healthy"] = False
            top_level_health["messages"].append(f"Unhealthy: {cmsg}")
        elif ctype == "CrdMisconfigured" and cstatus == "True":
            top_level_health["crdMisconfigured"] = True
            top_level_health["messages"].append(f"Misconfigured: {cmsg}")
        elif ctype == "UnableToProvision" and cstatus == "True":
            top_level_health["unableToProvision"] = True
            top_level_health["messages"].append(f"UnableToProvision: {cmsg}")

    priorities_status = []
    p_statuses = status.get("priorityStatuses", [])
    spec_rules = extract_priority_spec_rules(cc_obj)
    spec_rules_by_id = {r["identifier"]: r for r in spec_rules}
    p_status_by_id = {
        str(ps.get("identifier", "unknown")): ps for ps in p_statuses
    }

    if spec_rules:
        ordered_ids = [r["identifier"] for r in spec_rules]
    else:
        ordered_ids = [
            str(ps.get("identifier", "unknown"))
            for ps in p_statuses
            if str(ps.get("identifier", "")) != "ScaleUpAnyway"
        ]

    for ident_str in ordered_ids:
        ps = p_status_by_id.get(ident_str, {})
        rule_meta = spec_rules_by_id.get(ident_str, {})
        conds = ps.get("conditions", [])

        state = "Active"
        backoff_info = {"untilUtc": None, "remainingSeconds": 0}
        in_progress_info = None
        in_progress_nodes = 0
        messages_list = []
        reasons_list = []
        excluded_configs = None

        for c in conds:
            ctype = c.get("type", "")
            cmsg = c.get("message", "")
            creason = c.get("reason", "")
            if creason and creason not in reasons_list:
                reasons_list.append(creason)
            if cmsg and cmsg not in messages_list:
                messages_list.append(cmsg)

            m_cfg = EXCLUDED_CONFIGS_REGEX.search(cmsg)
            if m_cfg:
                excluded_configs = m_cfg.group(1)

            cand_backoff = parse_backoff_timestamp(cmsg, now)
            if cand_backoff.get("untilUtc") and (
                not backoff_info.get("untilUtc")
                or cand_backoff.get("remainingSeconds", 0)
                > backoff_info.get("remainingSeconds", 0)
            ):
                backoff_info = cand_backoff

            if ctype == "ProvisioningSuspended":
                state = "ProvisioningSuspended"
            elif ctype == "ProvisioningConstrained":
                if state != "ProvisioningSuspended":
                    state = "ProvisioningConstrained"
            elif ctype == "NodeProvisioningInProgress":
                in_progress_info = cmsg
                m = IN_PROGRESS_NODES_REGEX.search(cmsg)
                in_progress_nodes = int(m.group(1)) if m else 1
                if state == "Active":
                    state = "NodeProvisioningInProgress"

        state_message = " | ".join(messages_list)

        # Disambiguate primary GKE condition reason
        combined_text = (" ".join(reasons_list) + " " + state_message).lower()
        if "quotaexceeded" in combined_text or "quota_exceeded" in combined_text or "c3_cpus" in combined_text:
            primary_reason = "QuotaExceeded"
        elif "outofresources" in combined_text or "resource_pool_exhausted" in combined_text:
            primary_reason = "OutOfResources"
        elif "capacityconstrained" in combined_text:
            primary_reason = "CapacityConstrained"
        elif reasons_list:
            primary_reason = reasons_list[0]
        else:
            primary_reason = ""

        # Resource Info
        res_info = ps.get("resourceInfo", [])
        cpu_info = {"current": 0, "target": 0, "utilizationPct": 0.0}
        for r in res_info:
            if r.get("name") == "cpu":
                cpu_info["current"] = r.get("currentCount", 0)
                cpu_info["target"] = r.get("targetCount", 0)
                cpu_info["utilizationPct"] = r.get(
                    "currentUtilizationPercentage", 0.0
                )

        # Scaling Events History (adjusted for any user reset baseline)
        events_hist = ps.get("scalingEventsHistory", {})
        base_ev = (events_baseline or {}).get(ident_str, {})
        raw_prov = events_hist.get("provisionedNodesCount", 0)
        raw_cons = events_hist.get("consolidatedNodesCount", 0)
        raw_migr = events_hist.get("migratedNodesCount", 0)

        priorities_status.append(
            {
                "identifier": ident_str,
                "family": rule_meta.get("family", ""),
                "name": rule_meta.get("name", f"Tier {ident_str}"),
                "machineType": rule_meta.get("machineType", ""),
                "coresPerNode": rule_meta.get("coresPerNode", 8),
                "note": rule_meta.get("note", ""),
                "state": state,
                "reason": primary_reason,
                "reasons": reasons_list,
                "excludedConfigs": excluded_configs,
                "stateMessage": state_message,
                "backoff": backoff_info,
                "inProgress": in_progress_info,
                "inProgressNodes": in_progress_nodes,
                "cpu": cpu_info,
                "scalingEvents": {
                    "provisioned": max(
                        0, raw_prov - base_ev.get("provisioned", 0)
                    ),
                    "consolidated": max(
                        0, raw_cons - base_ev.get("consolidated", 0)
                    ),
                    "migrated": max(0, raw_migr - base_ev.get("migrated", 0)),
                },
                "rawScalingEvents": {
                    "provisioned": raw_prov,
                    "consolidated": raw_cons,
                    "migrated": raw_migr,
                },
            }
        )

    return {"topLevel": top_level_health, "priorities": priorities_status}


_pod_latency_cache: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}


def compute_pod_latencies(
    pods: List[Dict[str, Any]],
    now: Optional[datetime] = None,
    min_created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Computes scheduling latency (PodScheduled - created) and microVM creation latency (ContainersReady - PodScheduled)."""
    sched_latencies = []
    boot_latencies = []

    if len(_pod_latency_cache) > 5000:
        _pod_latency_cache.clear()

    min_ts = min_created_at.timestamp() if min_created_at else None

    for pod in pods:
        meta = pod.get("metadata", {})
        pod_uid = meta.get("uid") or meta.get("name")
        res_ver = meta.get("resourceVersion")
        cache_key = (
            (pod_uid, res_ver, str(min_ts))
            if (pod_uid and res_ver)
            else None
        )

        if cache_key and cache_key in _pod_latency_cache:
            sched_sec, boot_sec = _pod_latency_cache[cache_key]
            if sched_sec is not None:
                sched_latencies.append(sched_sec)
                if boot_sec is not None:
                    boot_latencies.append(boot_sec)
            continue

        status = pod.get("status", {})
        created_str = meta.get("creationTimestamp")
        if not created_str:
            continue

        try:
            created_dt = datetime.fromisoformat(
                created_str.replace("Z", "+00:00")
            )
        except Exception:
            continue

        if min_created_at and created_dt < min_created_at:
            if cache_key:
                _pod_latency_cache[cache_key] = (None, None)
            continue

        scheduled_dt = None
        ready_dt = None

        for cond in status.get("conditions", []):
            ctype = cond.get("type")
            cstatus = cond.get("status")
            ltt = cond.get("lastTransitionTime")
            if not ltt or cstatus != "True":
                continue
            try:
                trans_dt = datetime.fromisoformat(ltt.replace("Z", "+00:00"))
            except Exception:
                continue

            if ctype == "PodScheduled":
                scheduled_dt = trans_dt
            elif ctype == "ContainersReady":
                ready_dt = trans_dt

        sched_sec = None
        boot_sec = None
        if scheduled_dt:
            sched_sec = max(0.0, (scheduled_dt - created_dt).total_seconds())
            sched_latencies.append(sched_sec)
            if ready_dt and ready_dt >= scheduled_dt:
                boot_sec = max(0.0, (ready_dt - scheduled_dt).total_seconds())
                boot_latencies.append(boot_sec)

        if cache_key:
            _pod_latency_cache[cache_key] = (sched_sec, boot_sec)

    def calc_stats(lat_list: List[float]) -> Dict[str, float]:
        if not lat_list:
            return {"avg": 0.0, "p50": 0.0, "p95": 0.0}
        s = sorted(lat_list)
        avg = sum(s) / len(s)
        p50 = s[int(len(s) * 0.5)]
        p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
        return {
            "avg": round(avg, 2),
            "p50": round(p50, 2),
            "p95": round(p95, 2),
        }

    return {
        "scheduling": calc_stats(sched_latencies),
        "microvmBoot": calc_stats(boot_latencies),
        "sampleCount": len(sched_latencies),
    }


def distribute_standby_replicas(
    total_standby: int, families: Optional[List[str]] = None
) -> Dict[str, int]:
    """Distributes total standby replicas across active machine families dynamically."""
    total = max(0, int(total_standby))
    fams = [f for f in (families or DEFAULT_FAMILIES) if f]
    if not fams:
        fams = list(DEFAULT_FAMILIES)
    # Deduplicate while preserving priority order
    unique_fams = list(dict.fromkeys(fams))
    k = len(unique_fams)
    base = total // k
    rem = total % k

    if unique_fams == DEFAULT_FAMILIES:
        order = ["n2", "n4", "c2", "c4", "c3"]
    else:
        # Favor fallback / floor tiers first when distributing remainder
        order = list(reversed(unique_fams))

    result = {fam: base for fam in unique_fams}
    for i in range(rem):
        result[order[i % k]] += 1
    return result


def aggregate_capacity_buffers(
    buffers: Optional[List[Dict[str, Any]]] = None,
    single_buffer: Optional[Dict[str, Any]] = None,
    suspended_nodes: int = 0,
    families: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Aggregates active (buffer.x-k8s.io/active-capacity) and multi-family standby (buffer.gke.io/standby-capacity) buffers."""
    items_list = []
    if buffers:
        items_list = buffers
    elif single_buffer:
        items_list = [single_buffer]

    fam_list = list(dict.fromkeys(families or DEFAULT_FAMILIES))
    active_replicas = 0
    standby_replicas = 0
    standby_by_family: Dict[str, int] = {fam: 0 for fam in fam_list}
    ready_buffers = 0
    provisioning_buffers = 0
    buffer_details = []

    for b in items_list:
        meta = b.get("metadata", {})
        b_name = meta.get("name", "buffer")
        b_labels = meta.get("labels", {})
        spec = b.get("spec", {})
        status = b.get("status", {})

        reps = int(spec.get("replicas", 0))
        strat = spec.get(
            "provisioningStrategy", "buffer.x-k8s.io/active-capacity"
        )
        fam = str(b_labels.get("machine-family", "")).strip().lower()
        if not fam and "-" in b_name:
            suffix = b_name.rsplit("-", 1)[-1].lower()
            if GCE_FAMILY_PREFIX_REGEX.match(suffix):
                fam = suffix

        is_ready = False
        is_prov = False
        for cond in status.get("conditions", []):
            if (
                cond.get("type") == "ReadyForProvisioning"
                and cond.get("status") == "True"
            ):
                is_ready = True
            elif (
                cond.get("type") == "Provisioning"
                and cond.get("status") == "True"
            ):
                is_prov = True
        if status.get("status") == "ReadyForProvisioning":
            is_ready = True

        if is_ready:
            ready_buffers += 1
        if is_prov:
            provisioning_buffers += 1

        if strat == "buffer.gke.io/standby-capacity":
            standby_replicas += reps
            if fam:
                standby_by_family[fam] = standby_by_family.get(fam, 0) + reps
        else:
            active_replicas += reps

        buffer_details.append(
            {
                "name": b_name,
                "strategy": strat,
                "family": fam or "any",
                "replicas": reps,
                "ready": is_ready,
                "provisioning": is_prov,
            }
        )

    if not items_list:
        active_replicas = demo_state.get("activeBufferReplicas", 2)
        standby_replicas = demo_state.get("standbyReplicas", 6)
        standby_by_family = distribute_standby_replicas(
            standby_replicas, fam_list
        )
        ready_buffers = len(fam_list) + 1

    return {
        "configuredReplicas": (
            standby_replicas if standby_replicas > 0 else active_replicas
        ),
        "activeReplicas": active_replicas,
        "standbyReplicas": standby_replicas,
        "standbyByFamily": standby_by_family,
        "suspendedNodes": suspended_nodes,
        "readyBuffers": ready_buffers,
        "provisioningBuffers": provisioning_buffers,
        "totalBuffers": len(items_list) if items_list else (len(fam_list) + 1),
        "strategy": (
            "hybrid (active + multi-family standby)"
            if (active_replicas > 0 and standby_replicas > 0)
            else "buffer.gke.io/standby-capacity"
        ),
        "status": "ReadyForProvisioning" if ready_buffers > 0 else "Reconciling",
        "items": buffer_details,
    }


# ---------------------------------------------------------------------------
# Aggregation & State Assembly
# ---------------------------------------------------------------------------


def assemble_telemetry(
    nodes: List[Dict[str, Any]],
    pods: List[Dict[str, Any]],
    cc_obj: Optional[Dict[str, Any]],
    quotas: List[Dict[str, Any]],
    buffer_obj: Optional[Dict[str, Any]] = None,
    target_agents: int = 0,
    actors_per_worker: int = 2,
    now: Optional[datetime] = None,
    buffers: Optional[List[Dict[str, Any]]] = None,
    cluster_name: str = "mock-simulation",
    sandbox_runtime: str = "microvm",
) -> Dict[str, Any]:
    """Assembles all telemetry into the unified dashboard payload."""
    if now is None:
        now = datetime.now(timezone.utc)

    rt_cfg = get_runtime_config(sandbox_runtime)

    # 1. Parse ComputeClass first to discover dynamic priority tiers & machine families
    cc_status = (
        parse_compute_class_status(
            cc_obj,
            now,
            events_baseline=demo_state.get("scaling_events_baseline"),
        )
        if cc_obj
        else {
            "topLevel": {
                "healthy": True,
                "consolidationDelayMinutes": demo_state.get(
                    "consolidationDelayMinutes", 1
                ),
                "messages": [],
            },
            "priorities": [],
        }
    )

    discovered_families: List[str] = []
    for idx, p in enumerate(cc_status.get("priorities", [])):
        pfam = p.get("family") or (
            DEFAULT_FAMILIES[idx] if idx < len(DEFAULT_FAMILIES) else f"tier{idx}"
        )
        p["family"] = pfam
        if not p.get("name") or p["name"] == f"Tier {p['identifier']}":
            p["name"] = f"Tier {p['identifier']}: {pfam}"
        if pfam not in discovered_families:
            discovered_families.append(pfam)

    if not discovered_families:
        discovered_families = list(DEFAULT_FAMILIES)

    # 2. Node aggregation by dynamic family, priority annotation, and standby suspension
    # Only active (non-suspended) nodes are counted in nodes_by_family so sleeping S3 standby VMs
    # are not lumped in with active provisioned capacity or vCPU quota usage.
    nodes_by_family: Dict[str, int] = {fam: 0 for fam in discovered_families}
    nodes_by_family["other"] = 0
    suspended_by_family: Dict[str, int] = {fam: 0 for fam in discovered_families}
    nodes_by_annotation: Dict[str, int] = {
        str(i): 0 for i in range(max(5, len(cc_status.get("priorities", []))))
    }
    nodes_by_annotation["ccc_scale_up_anyway"] = 0
    nodes_by_annotation["pending_annotation"] = 0
    nodes_by_annotation["unannotated"] = 0

    total_ready_nodes = 0
    suspended_nodes = 0

    for n in nodes:
        fam = extract_machine_family(n)
        if is_node_suspended(n):
            suspended_nodes += 1
            if fam and fam != "unknown":
                if fam not in suspended_by_family:
                    suspended_by_family[fam] = 0
                suspended_by_family[fam] += 1
            continue

        if fam and fam != "unknown":
            if fam not in nodes_by_family:
                nodes_by_family[fam] = 0
                if fam not in discovered_families:
                    discovered_families.append(fam)
            nodes_by_family[fam] += 1
        else:
            nodes_by_family["other"] += 1

        ann = extract_priority_index(n, now)
        if ann in nodes_by_annotation:
            nodes_by_annotation[ann] += 1
        else:
            nodes_by_annotation["unannotated"] += 1

        # Check Ready condition on active nodes
        for cond in n.get("status", {}).get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                total_ready_nodes += 1
                break

    total_nodes = max(0, len(nodes) - suspended_nodes)

    # 3. Worker Pods & Active Agents aggregation
    target_microvms = (
        (target_agents + actors_per_worker - 1) // actors_per_worker
        if target_agents > 0
        else 0
    )
    ready_microvms = 0
    pending_microvms = 0

    for p in pods:
        p_phase = p.get("status", {}).get("phase", "")
        if p_phase == "Running":
            ready_microvms += 1
        elif p_phase == "Pending":
            pending_microvms += 1

    active_agents = ready_microvms * actors_per_worker

    consolidation_delay_mins = cc_status.get("topLevel", {}).get(
        "consolidationDelayMinutes",
        demo_state.get("consolidationDelayMinutes", 1),
    )
    in_progress_total = sum(
        p.get("inProgressNodes", 0) for p in cc_status.get("priorities", [])
    )

    # 4. Latency
    reset_at = demo_state.get("metrics_reset_at")
    latencies = compute_pod_latencies(
        pods, now, min_created_at=reset_at
    )
    since_dt = reset_at or demo_state.get("started_at") or now
    latencies["metricsSince"] = since_dt.strftime("%H:%M:%S UTC")

    # 5. CapacityQuotas (dynamic family & class-wide matching)
    quota_metrics = {}
    family_quotas_map: Dict[str, Dict[str, Any]] = {}
    class_quota_entry: Optional[Dict[str, Any]] = None

    for q in quotas:
        q_name = q.get("metadata", {}).get("name", "")
        q_limit = int(
            q.get("spec", {})
            .get("limits", {})
            .get("resources", {})
            .get("cpu", 0)
        )
        selector = q.get("spec", {}).get("selector", {})
        match_labels = selector.get("matchLabels", {})
        match_exprs = selector.get("matchExpressions", [])

        q_fam = match_labels.get("cloud.google.com/machine-family")
        expr_fams: List[str] = []
        for expr in match_exprs:
            if (
                expr.get("key") == "cloud.google.com/machine-family"
                and expr.get("operator") == "In"
            ):
                expr_fams.extend(
                    str(v).strip().lower() for v in expr.get("values", [])
                )

        if q_fam:
            q_fam_clean = str(q_fam).strip().lower()
            used_cpu = nodes_by_family.get(q_fam_clean, 0) * 8
            scope = "family"
            target_fams = [q_fam_clean]
        elif expr_fams:
            used_cpu = sum(nodes_by_family.get(f, 0) * 8 for f in expr_fams)
            scope = "family"
            target_fams = expr_fams
        else:
            used_cpu = total_nodes * 8
            scope = "class"
            target_fams = []

        pct = round((used_cpu / q_limit * 100) if q_limit > 0 else 0.0, 1)
        q_entry = {
            "name": q_name,
            "scope": scope,
            "families": target_fams,
            "limitCpu": q_limit,
            "usedCpu": used_cpu,
            "utilizationPct": pct,
            "capped": q_limit > 0 and used_cpu >= q_limit,
        }
        quota_metrics[q_name] = q_entry
        if scope == "family":
            for tf in target_fams:
                family_quotas_map[tf] = q_entry
        elif scope == "class" and class_quota_entry is None:
            class_quota_entry = q_entry

    # Enrich each ComputeClass priority tier with unified quotaInfo & disambiguated status cause
    priorities_list = cc_status.get("priorities", [])
    for idx, p in enumerate(priorities_list):
        pfam = p.get("family", "")
        cores_per_node = int(p.get("coresPerNode", 8) or 8)
        fam_used_cpu = nodes_by_family.get(pfam, 0) * cores_per_node
        tier_role = "Primary tier" if idx == 0 else "Fallback tier"

        if pfam in family_quotas_map:
            fq = family_quotas_map[pfam]
            q_info = {
                "name": fq["name"],
                "scope": "family",
                "scopeLabel": f"CapacityQuota ({fq['name']})",
                "usedCpu": fq["usedCpu"],
                "tierUsedCpu": fam_used_cpu,
                "limitCpu": fq["limitCpu"],
                "utilizationPct": fq["utilizationPct"],
                "capped": fq["capped"],
            }
            if not p.get("note") or "tier" in p.get("note", "").lower():
                p["note"] = f"{tier_role} · CapacityQuota ({fq['limitCpu']} vCPU)"
        elif p.get("reason") == "QuotaExceeded":
            # Dynamically infer regional GCE CPU quota ceiling when Cluster Autoscaler reports QuotaExceeded
            msg_limit_match = GCE_QUOTA_LIMIT_REGEX.search(
                p.get("stateMessage", "") or p.get("message", "")
            )
            reported_target = int(p.get("cpu", {}).get("target", 0) or 0)
            if msg_limit_match:
                gce_limit = int(float(msg_limit_match.group(1)))
            elif reported_target > 0:
                gce_limit = reported_target
            else:
                gce_limit = max(fam_used_cpu, cores_per_node)
            pct = round(
                (fam_used_cpu / gce_limit * 100) if gce_limit > 0 else 100.0, 1
            )
            q_info = {
                "name": f"{pfam.upper()}_CPUS",
                "scope": "gce_project",
                "scopeLabel": f"GCE Quota ({pfam.upper()}_CPUS)",
                "usedCpu": fam_used_cpu,
                "tierUsedCpu": fam_used_cpu,
                "limitCpu": gce_limit,
                "utilizationPct": pct,
                "capped": True,
            }
            p["note"] = (
                f"{tier_role} · GCE {pfam.upper()}_CPUS:{gce_limit} regional quota"
            )
        elif class_quota_entry is not None:
            cq = class_quota_entry
            q_info = {
                "name": cq["name"],
                "scope": "class",
                "scopeLabel": f"Class Cap ({pfam}: {fam_used_cpu}c / all tiers: {cq['usedCpu']}c)",
                "usedCpu": cq["usedCpu"],
                "tierUsedCpu": fam_used_cpu,
                "limitCpu": cq["limitCpu"],
                "utilizationPct": cq["utilizationPct"],
                "capped": cq["capped"],
            }
            p["note"] = (
                f"{tier_role} · Class cap ({cq['limitCpu']} vCPU)"
                if idx == 0
                else f"Elastic catch-all floor · Class cap ({cq['limitCpu']} vCPU)"
            )
        else:
            q_info = None

        p["quotaInfo"] = q_info
        is_k8s_quota_capped = bool(
            q_info and q_info["capped"] and q_info["scope"] in ("family", "class")
        )
        is_gce_quota_capped = bool(
            (q_info and q_info["capped"] and q_info["scope"] == "gce_project")
            or p.get("reason") == "QuotaExceeded"
        )
        p["isQuotaCapped"] = is_k8s_quota_capped
        p["isGceQuotaCapped"] = is_gce_quota_capped

        # Disambiguated badge & cause summary (Stockout vs. GCE Quota vs. CapacityQuota)
        p_state = p.get("state", "Active")
        p_reason = p.get("reason", "")
        excl = p.get("excludedConfigs")
        excl_tag = f" ({excl} configs)" if excl else ""

        if p_state == "ProvisioningSuspended":
            if p_reason == "QuotaExceeded" or is_gce_quota_capped:
                p["badgeLabel"] = "SUSPENDED · GCE QUOTA"
                p["secondaryBadge"] = (
                    f"CAPPED · CAPACITYQUOTA ({q_info['usedCpu']}/{q_info['limitCpu']}c)"
                    if is_k8s_quota_capped and q_info
                    else None
                )
                p["causeSummary"] = (
                    f"Full tier backoff — GCE regional quota ({pfam.upper()}_CPUS) exhausted"
                )
            else:
                p["badgeLabel"] = "SUSPENDED · STOCKOUT"
                p["secondaryBadge"] = (
                    f"CAPPED · CAPACITYQUOTA ({q_info['usedCpu']}/{q_info['limitCpu']}c)"
                    if is_k8s_quota_capped and q_info
                    else None
                )
                p["causeSummary"] = (
                    "Full tier backoff — GCE hardware stockout in all zones"
                )
        elif p_state == "ProvisioningConstrained":
            if is_k8s_quota_capped and q_info:
                p["badgeLabel"] = "CAPPED · CAPACITYQUOTA"
                p["secondaryBadge"] = f"CONSTRAINED · STOCKOUT{excl_tag}"
                p["causeSummary"] = (
                    f"CapacityQuota reached ({q_info['usedCpu']}/{q_info['limitCpu']}c) — spilling over (also partial zonal stockout{excl_tag})"
                )
            elif p_reason == "QuotaExceeded" or is_gce_quota_capped:
                p["badgeLabel"] = f"CONSTRAINED · GCE QUOTA{excl_tag}"
                p["secondaryBadge"] = None
                p["causeSummary"] = (
                    f"GCE regional quota reached ({q_info['usedCpu']}/{q_info['limitCpu']}c) + zonal cooldown{excl_tag}"
                    if q_info
                    else f"GCE regional quota / zonal cooldown{excl_tag}"
                )
            else:
                p["badgeLabel"] = f"CONSTRAINED · STOCKOUT{excl_tag}"
                p["secondaryBadge"] = None
                p["causeSummary"] = (
                    f"Partial zonal stockout{excl_tag} — healthy zones still scaling"
                )
        elif is_k8s_quota_capped and q_info:
            p["badgeLabel"] = "CAPPED · CAPACITYQUOTA"
            p["secondaryBadge"] = None
            p["causeSummary"] = (
                f"Pre-flight CapacityQuota limit reached ({q_info['usedCpu']}/{q_info['limitCpu']}c) — spilling over to next tier"
            )
        elif is_gce_quota_capped and q_info:
            p["badgeLabel"] = "CAPPED · GCE QUOTA"
            p["secondaryBadge"] = None
            p["causeSummary"] = (
                f"GCE regional quota limit reached ({q_info['usedCpu']}/{q_info['limitCpu']}c) — spilling over to next tier"
            )
        elif p_state == "NodeProvisioningInProgress":
            p["badgeLabel"] = "PROVISIONING"
            p["secondaryBadge"] = None
            p["causeSummary"] = "GCE VMs currently booting in this tier"
        else:
            p["badgeLabel"] = "Active"
            p["secondaryBadge"] = None
            p["causeSummary"] = "All zones & quotas available for scale-up"

    # 6. Active + Multi-Family Standby CapacityBuffers
    buffer_status = aggregate_capacity_buffers(
        buffers=buffers,
        single_buffer=buffer_obj,
        suspended_nodes=suspended_nodes,
        families=discovered_families,
    )

    # 7. Rule-0 vs. Fallback Share (use annotated "0" or dynamic Rule-0 family vs fallback families)
    rule0_fam = discovered_families[0] if discovered_families else "c3"
    rule0_nodes = max(
        nodes_by_annotation.get("0", 0), nodes_by_family.get(rule0_fam, 0)
    )
    fallback_nodes = max(0, total_nodes - rule0_nodes)
    rule0_share = (
        round(rule0_nodes / total_nodes * 100, 1) if total_nodes > 0 else 100.0
    )
    fallback_share = (
        round(fallback_nodes / total_nodes * 100, 1) if total_nodes > 0 else 0.0
    )

    return {
        "timestamp": now.isoformat(),
        "summary": {
            "clusterName": cluster_name,
            "sandboxRuntime": rt_cfg["runtime"],
            "sandboxRuntimeLabel": rt_cfg["label"],
            "computeClassName": rt_cfg["computeClass"],
            "targetAgents": target_agents,
            "activeAgents": active_agents,
            "actorsPerWorker": actors_per_worker,
            "targetSandboxes": target_microvms,
            "readySandboxes": ready_microvms,
            "pendingSandboxes": pending_microvms,
            "targetMicrovms": target_microvms,
            "readyMicrovms": ready_microvms,
            "pendingMicrovms": pending_microvms,
            "totalNodes": total_nodes,
            "readyNodes": total_ready_nodes,
            "suspendedNodes": suspended_nodes,
            "suspendedByFamily": suspended_by_family,
            "inProgressNodes": in_progress_total,
            "consolidationDelayMinutes": consolidation_delay_mins,
            "rule0Share": rule0_share,
            "fallbackShare": fallback_share,
            "familyOrder": discovered_families,
            "nodesByFamily": nodes_by_family,
            "nodesByAnnotation": nodes_by_annotation,
        },
        "computeClass": cc_status,
        "quotas": quota_metrics,
        "buffer": buffer_status,
        "latencies": latencies,
    }


# ---------------------------------------------------------------------------
# Mock Simulator (for local testing and verification without live cluster)
# ---------------------------------------------------------------------------


def generate_mock_cluster_data(
    target_agents: int,
    actors_per_worker: int,
    quotas: Dict[str, int],
    now: Optional[datetime] = None,
    sandbox_runtime: Optional[str] = None,
) -> Dict[str, Any]:
    """Simulates realistic cluster node provisioning and cascade down the 5-tier ladder."""
    if now is None:
        now = datetime.now(timezone.utc)

    rt_cfg = get_runtime_config(sandbox_runtime)

    target_pods = (
        (target_agents + actors_per_worker - 1) // actors_per_worker
        if target_agents > 0
        else 0
    )

    # Each standard-8 node packs 25 microVM pods
    needed_nodes = (target_pods + 24) // 25 if target_pods > 0 else 0

    rem = needed_nodes
    c3_nodes = min(rem, quotas.get("c3-tier-cap", 24) // 8)
    rem -= c3_nodes
    n4_nodes = min(rem, quotas.get("n4-tier-cap", 96) // 8)
    rem -= n4_nodes
    c4_nodes = min(rem, quotas.get("c4-tier-cap", 96) // 8)
    rem -= c4_nodes
    c2_nodes = min(rem, quotas.get("c2-tier-cap", 96) // 8)
    rem -= c2_nodes
    n2_nodes = rem

    simulated_nodes = []
    node_id = 0

    def add_nodes(count: int, fam: str, ann: str):
        nonlocal node_id
        for _ in range(count):
            node_id += 1
            simulated_nodes.append(
                {
                    "metadata": {
                        "name": f"gke-ccc-agents-{fam}-std8-{node_id:04d}",
                        "creationTimestamp": now.isoformat(),
                        "labels": {
                            "cloud.google.com/machine-family": fam,
                            "node.kubernetes.io/instance-type": (
                                f"{fam}-standard-8"
                            ),
                            "cloud.google.com/compute-class": (
                                rt_cfg["computeClass"]
                            ),
                        },
                        "annotations": {
                            "cloud.google.com/ccc_priority_index": ann
                        },
                    },
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                }
            )

    add_nodes(c3_nodes, "c3", "0")
    add_nodes(n4_nodes, "n4", "1")
    add_nodes(c4_nodes, "c4", "2")
    add_nodes(c2_nodes, "c2", "3")
    add_nodes(n2_nodes, "n2", "4")

    simulated_pods = []
    for i in range(target_pods):
        simulated_pods.append(
            {
                "metadata": {
                    "name": f"agent-{rt_cfg['runtime']}-worker-{i:05d}",
                    "creationTimestamp": now.isoformat(),
                },
                "status": {
                    "phase": "Running",
                    "conditions": [
                        {
                            "type": "PodScheduled",
                            "status": "True",
                            "lastTransitionTime": now.isoformat(),
                        },
                        {
                            "type": "ContainersReady",
                            "status": "True",
                            "lastTransitionTime": now.isoformat(),
                        },
                    ],
                },
            }
        )

    c3_suspended = needed_nodes >= 3
    cc_mock = {
        "metadata": {"name": rt_cfg["computeClass"]},
        "spec": {
            "autoscalingPolicy": {
                "consolidationDelayMinutes": demo_state.get(
                    "consolidationDelayMinutes", 1
                )
            },
            "priorities": [
                {"machineType": "c3-standard-8", "maxPodsPerNode": 32},
                {"machineType": "n4-standard-8", "maxPodsPerNode": 32},
                {"machineType": "c4-standard-8", "maxPodsPerNode": 32},
                {"machineType": "c2-standard-8", "maxPodsPerNode": 32},
                {
                    "machineFamily": "n2",
                    "minCores": 8,
                    "minMemoryGb": 32,
                    "maxPodsPerNode": 32,
                },
            ],
        },
        "status": {
            "conditions": [
                {
                    "type": "Health",
                    "status": "True",
                    "message": "Crd is healthy.",
                }
            ],
            "priorityStatuses": [
                {
                    "identifier": "0",
                    "conditions": (
                        [
                            {
                                "type": "ProvisioningSuspended",
                                "reason": "QuotaExceeded",
                                "message": "NodeProvisioning associated with this priority failed due to QuotaExceeded (C3_CPUS). Backing off the priority until 2026-10-23 23:59:59 UTC.",
                            }
                        ]
                        if c3_suspended
                        else []
                    ),
                    "resourceInfo": [
                        {
                            "name": "cpu",
                            "currentCount": c3_nodes * 8,
                            "targetCount": 24,
                            "currentUtilizationPercentage": 100.0
                            if c3_nodes == 3
                            else (c3_nodes * 8 / 24 * 100),
                        }
                    ],
                    "scalingEventsHistory": {
                        "provisionedNodesCount": c3_nodes,
                        "consolidatedNodesCount": 0,
                        "migratedNodesCount": 0,
                    },
                },
                {
                    "identifier": "1",
                    "conditions": [],
                    "resourceInfo": [
                        {
                            "name": "cpu",
                            "currentCount": n4_nodes * 8,
                            "targetCount": 96,
                            "currentUtilizationPercentage": round(
                                n4_nodes * 8 / 96 * 100, 1
                            ),
                        }
                    ],
                    "scalingEventsHistory": {
                        "provisionedNodesCount": n4_nodes,
                        "consolidatedNodesCount": 0,
                        "migratedNodesCount": 0,
                    },
                },
                {
                    "identifier": "2",
                    "conditions": [],
                    "resourceInfo": [
                        {
                            "name": "cpu",
                            "currentCount": c4_nodes * 8,
                            "targetCount": 96,
                            "currentUtilizationPercentage": round(
                                c4_nodes * 8 / 96 * 100, 1
                            ),
                        }
                    ],
                    "scalingEventsHistory": {
                        "provisionedNodesCount": c4_nodes,
                        "consolidatedNodesCount": 0,
                        "migratedNodesCount": 0,
                    },
                },
                {
                    "identifier": "3",
                    "conditions": [],
                    "resourceInfo": [
                        {
                            "name": "cpu",
                            "currentCount": c2_nodes * 8,
                            "targetCount": 96,
                            "currentUtilizationPercentage": round(
                                c2_nodes * 8 / 96 * 100, 1
                            ),
                        }
                    ],
                    "scalingEventsHistory": {
                        "provisionedNodesCount": c2_nodes,
                        "consolidatedNodesCount": 0,
                        "migratedNodesCount": 0,
                    },
                },
                {
                    "identifier": "4",
                    "conditions": [],
                    "resourceInfo": [
                        {
                            "name": "cpu",
                            "currentCount": n2_nodes * 8,
                            "targetCount": max(n2_nodes * 8, 32),
                            "currentUtilizationPercentage": 100.0
                            if n2_nodes > 0
                            else 0.0,
                        }
                    ],
                    "scalingEventsHistory": {
                        "provisionedNodesCount": n2_nodes,
                        "consolidatedNodesCount": 0,
                        "migratedNodesCount": 0,
                    },
                },
            ],
        }
    }

    mock_quotas = [
        {
            "metadata": {"name": "agent-substrate-class-cap"},
            "spec": {
                "selector": {
                    "matchLabels": {
                        "cloud.google.com/compute-class": (
                            rt_cfg["computeClass"]
                        )
                    }
                },
                "limits": {
                    "resources": {
                        "cpu": quotas.get("agent-substrate-class-cap", 720)
                    }
                },
            },
        },
        {
            "metadata": {"name": "c3-tier-cap"},
            "spec": {
                "selector": {
                    "matchLabels": {
                        "cloud.google.com/compute-class": (
                            rt_cfg["computeClass"]
                        ),
                        "cloud.google.com/machine-family": "c3",
                    }
                },
                "limits": {"resources": {"cpu": quotas.get("c3-tier-cap", 24)}},
            },
        },
        {
            "metadata": {"name": "n4-tier-cap"},
            "spec": {
                "selector": {
                    "matchLabels": {
                        "cloud.google.com/compute-class": (
                            rt_cfg["computeClass"]
                        ),
                        "cloud.google.com/machine-family": "n4",
                    }
                },
                "limits": {"resources": {"cpu": quotas.get("n4-tier-cap", 96)}},
            },
        },
        {
            "metadata": {"name": "c4-tier-cap"},
            "spec": {
                "selector": {
                    "matchLabels": {
                        "cloud.google.com/compute-class": (
                            rt_cfg["computeClass"]
                        ),
                        "cloud.google.com/machine-family": "c4",
                    }
                },
                "limits": {"resources": {"cpu": quotas.get("c4-tier-cap", 96)}},
            },
        },
        {
            "metadata": {"name": "c2-tier-cap"},
            "spec": {
                "selector": {
                    "matchLabels": {
                        "cloud.google.com/compute-class": (
                            rt_cfg["computeClass"]
                        ),
                        "cloud.google.com/machine-family": "c2",
                    }
                },
                "limits": {"resources": {"cpu": quotas.get("c2-tier-cap", 96)}},
            },
        },
    ]

    standby_dist = distribute_standby_replicas(
        demo_state.get("standbyReplicas", 6)
    )
    mock_buffers = [
        {
            "metadata": {
                "name": "agent-active-buffer",
                "labels": {"buffer-strategy": "active"},
            },
            "spec": {
                "replicas": demo_state.get("activeBufferReplicas", 2),
                "provisioningStrategy": "buffer.x-k8s.io/active-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        },
        {
            "metadata": {
                "name": "agent-standby-buffer-c3",
                "labels": {"machine-family": "c3"},
            },
            "spec": {
                "replicas": standby_dist["c3"],
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        },
        {
            "metadata": {
                "name": "agent-standby-buffer-n4",
                "labels": {"machine-family": "n4"},
            },
            "spec": {
                "replicas": standby_dist["n4"],
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        },
        {
            "metadata": {
                "name": "agent-standby-buffer-c4",
                "labels": {"machine-family": "c4"},
            },
            "spec": {
                "replicas": standby_dist["c4"],
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        },
        {
            "metadata": {
                "name": "agent-standby-buffer-c2",
                "labels": {"machine-family": "c2"},
            },
            "spec": {
                "replicas": standby_dist["c2"],
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        },
        {
            "metadata": {
                "name": "agent-standby-buffer-n2",
                "labels": {"machine-family": "n2"},
            },
            "spec": {
                "replicas": standby_dist["n2"],
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        },
    ]

    return assemble_telemetry(
        nodes=simulated_nodes,
        pods=simulated_pods,
        cc_obj=cc_mock,
        quotas=mock_quotas,
        buffers=mock_buffers,
        target_agents=target_agents,
        actors_per_worker=actors_per_worker,
        now=now,
        sandbox_runtime=rt_cfg["runtime"],
    )


# ---------------------------------------------------------------------------
# Live Kubernetes Collector
# ---------------------------------------------------------------------------


from concurrent.futures import ThreadPoolExecutor

_k8s_executor = ThreadPoolExecutor(max_workers=5)


def _detect_cluster_name() -> str:
    """Detects a sanitized cluster name from CLUSTER_NAME env or active kubeconfig context."""
    global _cached_cluster_name
    if _cached_cluster_name:
        return _cached_cluster_name
    env_name = os.environ.get("CLUSTER_NAME", "").strip()
    if env_name:
        _cached_cluster_name = env_name
        return _cached_cluster_name
    try:
        from kubernetes import config

        _, active = config.list_kube_config_contexts()
        if active:
            raw = str(
                active.get("context", {}).get("cluster")
                or active.get("name")
                or ""
            ).strip()
            parts = raw.split("_")
            if len(parts) >= 4 and parts[0] == "gke":
                _cached_cluster_name = "_".join(parts[3:])
            elif raw:
                _cached_cluster_name = raw
            if _cached_cluster_name:
                return _cached_cluster_name
    except Exception:
        pass
    _cached_cluster_name = "in-cluster"
    return _cached_cluster_name


def _get_k8s_clients() -> Tuple[Any, Any, Any]:
    """Lazily initializes and caches Kubernetes API clients."""
    global _k8s_clients
    if _k8s_clients is not None:
        return _k8s_clients

    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()

    _k8s_clients = (
        client.CoreV1Api(),
        client.AppsV1Api(),
        client.CustomObjectsApi(),
    )
    return _k8s_clients


def get_live_telemetry() -> Optional[Dict[str, Any]]:
    """Queries the live Kubernetes cluster concurrently across 5 API endpoints."""
    try:
        rt_cfg = get_runtime_config(demo_state.get("sandboxRuntime", "microvm"))
        core_v1, _, custom_api = _get_k8s_clients()

        def _fetch_nodes():
            raw = core_v1.list_node(
                label_selector=f"cloud.google.com/compute-class={rt_cfg['computeClass']}",
                _preload_content=False,
            )
            return json.loads(raw.data).get("items", [])

        def _fetch_pods():
            raw = core_v1.list_namespaced_pod(
                namespace=DEMO_NAMESPACE,
                label_selector=f"app={rt_cfg['podLabel']}",
                _preload_content=False,
            )
            return json.loads(raw.data).get("items", [])

        def _fetch_cc():
            try:
                return custom_api.get_cluster_custom_object(
                    group="cloud.google.com",
                    version="v1",
                    plural="computeclasses",
                    name=rt_cfg["computeClass"],
                )
            except Exception as e:
                logger.debug(f"ComputeClass lookup failed: {e}")
                return None

        def _fetch_quotas():
            try:
                res = custom_api.list_cluster_custom_object(
                    group="autoscaling.x-k8s.io",
                    version="v1beta1",
                    plural="capacityquotas",
                )
                return res.get("items", [])
            except Exception as e:
                logger.debug(f"CapacityQuota lookup failed: {e}")
                return []

        def _fetch_buffers():
            try:
                res = custom_api.list_namespaced_custom_object(
                    group="autoscaling.x-k8s.io",
                    version="v1beta1",
                    namespace=DEMO_NAMESPACE,
                    plural="capacitybuffers",
                )
                return res.get("items", [])
            except Exception as e:
                logger.debug(f"CapacityBuffer list failed: {e}")
                return []

        f_nodes = _k8s_executor.submit(_fetch_nodes)
        f_pods = _k8s_executor.submit(_fetch_pods)
        f_cc = _k8s_executor.submit(_fetch_cc)
        f_quotas = _k8s_executor.submit(_fetch_quotas)
        f_buffers = _k8s_executor.submit(_fetch_buffers)

        node_list = f_nodes.result()
        pod_list = f_pods.result()
        cc_obj = f_cc.result()
        quotas = f_quotas.result()
        buffers = f_buffers.result()

        return assemble_telemetry(
            nodes=node_list,
            pods=pod_list,
            cc_obj=cc_obj,
            quotas=quotas,
            buffers=buffers,
            target_agents=demo_state["targetAgents"],
            actors_per_worker=demo_state["actorsPerWorker"],
            cluster_name=_detect_cluster_name(),
            sandbox_runtime=rt_cfg["runtime"],
        )
    except Exception as e:
        logger.debug(f"Live Kubernetes query error: {e}")
        return None


def get_current_telemetry() -> Dict[str, Any]:
    """Returns live telemetry if cluster is reachable, otherwise simulated data."""
    if not demo_state["mock_mode"]:
        live = get_live_telemetry()
        if live is not None:
            return live

    return generate_mock_cluster_data(
        target_agents=demo_state["targetAgents"],
        actors_per_worker=demo_state["actorsPerWorker"],
        quotas=demo_state["quotas"],
        sandbox_runtime=demo_state.get("sandboxRuntime", "microvm"),
    )


async def get_cached_or_fresh_telemetry() -> Dict[str, Any]:
    """Returns background-cached telemetry if fresh (<2.5s) and matching current state, else refreshes off-thread."""
    cached = demo_state.get("latest_telemetry")
    cached_ts = demo_state.get("latest_telemetry_ts", 0.0)
    if (
        cached is not None
        and (time.monotonic() - cached_ts) < 2.5
        and cached.get("summary", {}).get("targetAgents")
        == demo_state["targetAgents"]
        and cached.get("summary", {}).get("actorsPerWorker")
        == demo_state["actorsPerWorker"]
        and cached.get("summary", {}).get("sandboxRuntime")
        == demo_state.get("sandboxRuntime", "microvm")
        and cached.get("buffer", {}).get("activeReplicas")
        == demo_state.get("activeBufferReplicas", 2)
        and cached.get("buffer", {}).get("standbyReplicas")
        == demo_state.get("standbyReplicas", 6)
        and cached.get("summary", {}).get("consolidationDelayMinutes")
        == demo_state.get("consolidationDelayMinutes", 1)
    ):
        return cached

    ver_before = demo_state.get("state_version", 0)
    data = await asyncio.to_thread(get_current_telemetry)
    if demo_state.get("state_version", 0) == ver_before:
        demo_state["latest_telemetry"] = data
        demo_state["latest_telemetry_ts"] = time.monotonic()
    return data


# ---------------------------------------------------------------------------
# Background Loop for History Buffer
# ---------------------------------------------------------------------------


async def telemetry_background_sampler():
    """Background task polling telemetry off-thread and maintaining rolling history."""
    while True:
        try:
            ver_before = demo_state.get("state_version", 0)
            data = await asyncio.to_thread(get_current_telemetry)
            if demo_state.get("state_version", 0) == ver_before:
                demo_state["latest_telemetry"] = data
                demo_state["latest_telemetry_ts"] = time.monotonic()
            summary = data.get("summary", {})
            nodes_by_family = summary.get("nodesByFamily", {})
            lat = data.get("latencies", {})

            entry = {
                "timestamp": data.get("timestamp"),
                "targetAgents": summary.get("targetAgents", 0),
                "activeAgents": summary.get(
                    "activeAgents",
                    summary.get("readyMicrovms", 0)
                    * summary.get("actorsPerWorker", 2),
                ),
                "targetMicrovms": summary.get("targetMicrovms", 0),
                "readyMicrovms": summary.get("readyMicrovms", 0),
                "pendingMicrovms": summary.get("pendingMicrovms", 0),
                "targetSandboxes": summary.get(
                    "targetSandboxes", summary.get("targetMicrovms", 0)
                ),
                "readySandboxes": summary.get(
                    "readySandboxes", summary.get("readyMicrovms", 0)
                ),
                "pendingSandboxes": summary.get(
                    "pendingSandboxes", summary.get("pendingMicrovms", 0)
                ),
                "sandboxRuntime": summary.get("sandboxRuntime", "microvm"),
                "totalNodes": summary.get("totalNodes", 0),
                "readyNodes": summary.get("readyNodes", 0),
                "suspendedNodes": summary.get("suspendedNodes", 0),
                "inProgressNodes": summary.get("inProgressNodes", 0),
                "nodesByFamily": dict(nodes_by_family),
                "nodes_c3": nodes_by_family.get("c3", 0),
                "nodes_n4": nodes_by_family.get("n4", 0),
                "nodes_c4": nodes_by_family.get("c4", 0),
                "nodes_c2": nodes_by_family.get("c2", 0),
                "nodes_n2": nodes_by_family.get("n2", 0),
                "schedLatency": lat.get("scheduling", {}).get("avg", 0.0),
                "bootLatency": lat.get("microvmBoot", {}).get("avg", 0.0),
            }

            demo_state["history"].append(entry)
            if len(demo_state["history"]) > MAX_HISTORY_LEN:
                demo_state["history"].pop(0)

        except Exception as e:
            logger.error(f"Error in telemetry sampler: {e}")

        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# API Models & Endpoints
# ---------------------------------------------------------------------------


class ScaleRequest(BaseModel):
    targetAgents: int = Field(..., ge=0, le=2000)
    actorsPerWorker: int = Field(default=2, ge=1, le=4)
    sandboxRuntime: Optional[str] = Field(
        default=None, pattern="^(microvm|gvisor)$"
    )
    activeBufferReplicas: Optional[int] = Field(default=None, ge=0, le=50)
    standbyReplicas: Optional[int] = Field(default=None, ge=0, le=100)
    consolidationDelayMinutes: Optional[int] = Field(
        default=None, ge=1, le=1440
    )


class QuotaUpdateRequest(BaseModel):
    quotaName: str
    cpuLimit: int = Field(..., ge=8, le=2000)


@app.get("/api/state")
async def get_state():
    """Returns current state snapshot and history buffer."""
    telemetry = await get_cached_or_fresh_telemetry()
    return {
        "current": telemetry,
        "history": demo_state["history"],
        "resetEpoch": demo_state.get("resetEpoch", 0),
    }


@app.post("/api/reset")
async def reset_metrics():
    """Resets chart history, pod latency averages/percentiles, and scaling event baselines."""
    now_utc = datetime.now(timezone.utc)
    current = await get_cached_or_fresh_telemetry()
    baseline = {}
    for p in current.get("computeClass", {}).get("priorities", []):
        ident = str(p.get("identifier", ""))
        raw_ev = p.get("rawScalingEvents") or p.get("scalingEvents") or {}
        baseline[ident] = {
            "provisioned": raw_ev.get("provisioned", 0),
            "consolidated": raw_ev.get("consolidated", 0),
            "migrated": raw_ev.get("migrated", 0),
        }

    demo_state["metrics_reset_at"] = now_utc
    demo_state["scaling_events_baseline"] = baseline
    demo_state["history"].clear()
    _pod_latency_cache.clear()
    demo_state["resetEpoch"] = demo_state.get("resetEpoch", 0) + 1
    demo_state["state_version"] = demo_state.get("state_version", 0) + 1
    demo_state["latest_telemetry"] = None

    logger.info(
        f"Metrics and chart history reset at {now_utc.isoformat()} (epoch={demo_state['resetEpoch']})"
    )
    return {
        "status": "success",
        "metricsSince": now_utc.strftime("%H:%M:%S UTC"),
        "resetEpoch": demo_state["resetEpoch"],
    }


@app.post("/api/scale")
async def set_scale(req: ScaleRequest):
    """Adjusts agent target scale, packing ratio, and active/standby buffer replicas."""
    prev_runtime = demo_state.get("sandboxRuntime", "microvm")
    if req.sandboxRuntime is not None:
        demo_state["sandboxRuntime"] = req.sandboxRuntime

    rt_cfg = get_runtime_config(demo_state["sandboxRuntime"])
    other_cfg = get_runtime_config(
        "gvisor" if rt_cfg["runtime"] == "microvm" else "microvm"
    )
    runtime_switched = rt_cfg["runtime"] != prev_runtime

    demo_state["targetAgents"] = req.targetAgents
    demo_state["actorsPerWorker"] = req.actorsPerWorker
    demo_state["state_version"] = demo_state.get("state_version", 0) + 1
    demo_state["latest_telemetry"] = None
    if req.activeBufferReplicas is not None:
        demo_state["activeBufferReplicas"] = req.activeBufferReplicas
    if req.standbyReplicas is not None:
        demo_state["standbyReplicas"] = req.standbyReplicas
    if req.consolidationDelayMinutes is not None:
        demo_state["consolidationDelayMinutes"] = req.consolidationDelayMinutes

    target_pods = (
        (req.targetAgents + req.actorsPerWorker - 1) // req.actorsPerWorker
        if req.targetAgents > 0
        else 0
    )
    standby_dist = distribute_standby_replicas(
        demo_state.get("standbyReplicas", 6)
    )

    if not demo_state["mock_mode"]:
        try:
            _, apps_v1, custom_api = _get_k8s_clients()

            # When runtime switched, scale down inactive runtime's Deployment and WorkerPool to 0
            if runtime_switched:
                try:
                    apps_v1.patch_namespaced_deployment_scale(
                        name=other_cfg["deployment"],
                        namespace=DEMO_NAMESPACE,
                        body={"spec": {"replicas": 0}},
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to scale down inactive deployment {other_cfg['deployment']}: {e}"
                    )
                try:
                    custom_api.patch_namespaced_custom_object(
                        group="ate.dev",
                        version="v1alpha1",
                        namespace=DEMO_NAMESPACE,
                        plural="workerpools",
                        name=other_cfg["workerPool"],
                        body={"spec": {"replicas": 0}},
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to scale down inactive workerpool {other_cfg['workerPool']}: {e}"
                    )

            # Scale active runtime Deployment and WorkerPool
            try:
                apps_v1.patch_namespaced_deployment_scale(
                    name=rt_cfg["deployment"],
                    namespace=DEMO_NAMESPACE,
                    body={"spec": {"replicas": target_pods}},
                )
            except Exception as e:
                logger.debug(
                    f"Failed to patch active deployment {rt_cfg['deployment']}: {e}"
                )

            try:
                custom_api.patch_namespaced_custom_object(
                    group="ate.dev",
                    version="v1alpha1",
                    namespace=DEMO_NAMESPACE,
                    plural="workerpools",
                    name=rt_cfg["workerPool"],
                    body={
                        "spec": {
                            "replicas": target_pods,
                            "actorsPerWorker": req.actorsPerWorker,
                        }
                    },
                )
            except Exception:
                pass

            if req.activeBufferReplicas is not None or runtime_switched:
                try:
                    custom_api.patch_namespaced_custom_object(
                        group="autoscaling.x-k8s.io",
                        version="v1beta1",
                        namespace=DEMO_NAMESPACE,
                        plural="capacitybuffers",
                        name="agent-active-buffer",
                        body={
                            "spec": {
                                "podTemplateRef": {
                                    "name": f"{rt_cfg['bufferPrefix']}-template"
                                },
                                "replicas": demo_state["activeBufferReplicas"],
                            }
                        },
                    )
                except Exception as e:
                    logger.warning(f"Failed to patch agent-active-buffer: {e}")

            if req.standbyReplicas is not None or runtime_switched:
                def _patch_standby(fam: str, count: int):
                    try:
                        custom_api.patch_namespaced_custom_object(
                            group="autoscaling.x-k8s.io",
                            version="v1beta1",
                            namespace=DEMO_NAMESPACE,
                            plural="capacitybuffers",
                            name=f"agent-standby-buffer-{fam}",
                            body={
                                "spec": {
                                    "podTemplateRef": {
                                        "name": f"{rt_cfg['bufferPrefix']}-{fam}"
                                    },
                                    "replicas": count,
                                }
                            },
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to patch agent-standby-buffer-{fam}: {e}"
                        )

                futures = [
                    _k8s_executor.submit(_patch_standby, fam, count)
                    for fam, count in standby_dist.items()
                ]
                for f in futures:
                    f.result()

            if req.consolidationDelayMinutes is not None:
                for cc_name in (
                    rt_cfg["computeClass"],
                    other_cfg["computeClass"],
                ):
                    try:
                        custom_api.patch_cluster_custom_object(
                            group="cloud.google.com",
                            version="v1",
                            plural="computeclasses",
                            name=cc_name,
                            body={
                                "spec": {
                                    "autoscalingPolicy": {
                                        "consolidationDelayMinutes": (
                                            req.consolidationDelayMinutes
                                        )
                                    }
                                }
                            },
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to patch ComputeClass {cc_name} consolidationDelayMinutes: {e}"
                        )

            logger.info(
                f"Scaled live {rt_cfg['runtime']} Deployment/WorkerPool to {target_pods} pods ({req.targetAgents} agents), "
                f"activeBuffer={demo_state['activeBufferReplicas']}, standbyBuffer={demo_state['standbyReplicas']} ({standby_dist}), "
                f"consolidationDelayMinutes={demo_state['consolidationDelayMinutes']}"
            )
        except Exception as e:
            logger.debug(f"Live cluster scale patch skipped: {e}")

    return {
        "status": "success",
        "sandboxRuntime": rt_cfg["runtime"],
        "sandboxRuntimeLabel": rt_cfg["label"],
        "targetAgents": req.targetAgents,
        "actorsPerWorker": req.actorsPerWorker,
        "targetSandboxes": target_pods,
        "targetMicrovms": target_pods,
        "activeBufferReplicas": demo_state["activeBufferReplicas"],
        "standbyReplicas": demo_state["standbyReplicas"],
        "standbyByFamily": standby_dist,
        "consolidationDelayMinutes": demo_state["consolidationDelayMinutes"],
    }


@app.post("/api/quotas")
async def set_quota(req: QuotaUpdateRequest):
    """Adjusts CapacityQuota limits live."""
    if req.quotaName not in demo_state["quotas"]:
        raise HTTPException(
            status_code=400, detail=f"Unknown quota name {req.quotaName}"
        )

    demo_state["quotas"][req.quotaName] = req.cpuLimit
    demo_state["latest_telemetry"] = None

    if not demo_state["mock_mode"]:
        try:
            _, _, custom_api = _get_k8s_clients()
            custom_api.patch_cluster_custom_object(
                group="autoscaling.x-k8s.io",
                version="v1beta1",
                plural="capacityquotas",
                name=req.quotaName,
                body={"spec": {"limits": {"resources": {"cpu": req.cpuLimit}}}},
            )
            logger.info(f"Patched {req.quotaName} to {req.cpuLimit} CPU")
        except Exception as e:
            logger.debug(f"Live cluster quota patch skipped: {e}")

    return {
        "status": "success",
        "quota": req.quotaName,
        "newLimit": req.cpuLimit,
    }


@app.get("/api/stream")
async def stream_telemetry():
    """Streams live telemetry via Server-Sent Events (SSE) using native StreamingResponse."""

    async def event_generator():
        while True:
            data = await get_cached_or_fresh_telemetry()
            hist = demo_state["history"]
            latest_pt = hist[-1] if hist else None
            payload = json.dumps(
                {
                    "current": data,
                    "latestPoint": latest_pt,
                    "resetEpoch": demo_state.get("resetEpoch", 0),
                    "history": hist[-60:],
                }
            )
            yield f"event: telemetry\ndata: {payload}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Serve static web frontend
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse(
        "<h2>GKE Agent Substrate - scale performance control tower</h2>"
    )


def main():
    parser = argparse.ArgumentParser(
        description="GKE Agent Substrate - scale performance control tower"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8881, help="Bind port")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force mock simulation mode even if cluster is reachable",
    )
    parser.add_argument(
        "--runtime",
        choices=["microvm", "gvisor"],
        default="microvm",
        help="Initial sandbox runtime (microvm or gvisor)",
    )
    args = parser.parse_args()

    demo_state["sandboxRuntime"] = args.runtime

    if args.mock:
        demo_state["mock_mode"] = True
        logger.info("Running in forced mock simulation mode")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
