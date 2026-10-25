#!/usr/bin/env python3
"""FastAPI telemetry backend and claim reconciler for the Agent Sandbox scale demo."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import copy
from datetime import datetime, timezone
import json
import logging
import os
import re
import threading
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
logger = logging.getLogger("sbx-demo")

TITLE = "GKE Agent Sandbox - scale performance control tower"


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


app = FastAPI(title=TITLE, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# DEMO_MODE=stateless (default): every agent is a SandboxClaim against a SandboxWarmPool,
# on a wide fallback ladder (c3 / n4 / c4 / c2 / n2), with gVisor or microVM.
# DEMO_MODE=snapshots: every agent is a session Sandbox with GKE Pod Snapshots (gVisor),
# on a one-family class (n4-standard-8 / 16 / 4), with suspend and resume.
# DEMO_MODE=snapshots-rootfs: session Sandboxes with rootfs-only Pod Snapshots (gVisor), on
# the same wide multi-family class as stateless gVisor. Only root-filesystem files survive.
DEMO_MODE = os.environ.get("DEMO_MODE", "stateless").strip().lower()
if DEMO_MODE not in ("stateless", "snapshots", "snapshots-rootfs"):
    DEMO_MODE = "stateless"
# Modes whose agents are session Sandboxes with suspend/resume (not claims).
SESSION_MODES = ("snapshots", "snapshots-rootfs")
SESSIONS = DEMO_MODE in SESSION_MODES
# Whole-pod snapshots restore on one machine family only, so that mode's class is one family.
ONE_FAMILY = DEMO_MODE == "snapshots"

DEMO_NAMESPACE = os.environ.get("DEMO_NAMESPACE", "agent-sandbox-demo")
PART_OF_LABEL = "app.kubernetes.io/part-of"
PART_OF = "ccc-agent-sandbox"
RUNTIME_LABEL = "sbx-demo/runtime"
ROLE_LABEL = "sbx-demo/role"

EXT_GROUP = "extensions.agents.x-k8s.io"
AGENTS_GROUP = "agents.x-k8s.io"
SBX_VERSION = "v1beta1"
SNAP_GROUP = "podsnapshot.gke.io"
SNAP_VERSION = "v1"
BUFFER_GROUP = "autoscaling.x-k8s.io"
BUFFER_VERSION = "v1beta1"

# A warm-pool pod carries this label until a SandboxClaim adopts it.
WARM_POOL_LABEL = "agents.x-k8s.io/warm-pool-sandbox"
SESSION_TEMPLATE = "agent-session-template"
# The rootfs-mode session container logs one JSON line of its state every few seconds.
# The tower reads the last line before a suspend and after a resume to see what survived.
SESSION_CONTAINER = "agent-runtime"
SURVIVAL_TIMEOUT_SECONDS = 180.0

DEFAULT_FAMILIES = ["c3", "n4", "c4", "c2", "n2"]
SNAPSHOT_TYPES = ["n4-standard-8", "n4-standard-16", "n4-standard-4"]

RECONCILE_SECONDS = 5.0
# Claims or sessions created per reconcile pass, so a big surge ramps instead of
# flooding the API server.
MAX_CREATES_PER_PASS = int(os.environ.get("MAX_CREATES_PER_PASS", "100"))
SNAPSHOT_TIMEOUT_SECONDS = float(os.environ.get("SNAPSHOT_TIMEOUT_SECONDS", "600"))
MAX_LATENCY_SAMPLES = 2000

# In-memory target state
demo_state: Dict[str, Any] = {
    "targetAgents": 0,
    "sandboxRuntime": "microvm",
    "warmPoolReplicas": 10,
    "activeBufferReplicas": 2,
    "standbyReplicas": 6,
    "consolidationDelayMinutes": 1,
    "quotas": {
        "sbx-demo-class-cap": 720,
        "sbx-demo-c3-cap": 24,
        "sbx-demo-n4-cap": 96,
        "sbx-demo-c4-cap": 96,
        "sbx-demo-c2-cap": 96,
    },
    "history": [],  # Ring buffer of time-series samples (up to 60m)
    "mock_mode": False,
    "started_at": datetime.now(timezone.utc),
    "metrics_reset_at": None,
    "scaling_events_baseline": {},
    "resetEpoch": 0,
    "adopted": False,
    "lastReconcile": 0.0,
    "patched": {},
    # Snapshots mode: per-session suspend/resume bookkeeping.
    "sessionOps": {},  # name -> {"state": snapshotting|suspending|error, "error": str}
    "resumeStartedAt": {},  # name -> epoch the tower set operatingMode Running
    "resumeLatencies": [],
    "snapshotLatencies": [],
    "mockSuspended": False,
    # Rootfs mode: session state read from pod logs before suspend / after resume.
    "preSuspend": {},  # name -> state dict logged before the snapshot
    "pendingSurvival": {},  # name -> epoch the resume started, until probed
    "survival": {},  # name -> {"fileKept": bool, "memoryKept": bool}
    "mockResumed": False,
}

if SESSIONS:
    demo_state["sandboxRuntime"] = "gvisor"
    demo_state["warmPoolReplicas"] = 0
    demo_state["activeBufferReplicas"] = 1
    demo_state["standbyReplicas"] = 2
if ONE_FAMILY:
    demo_state["quotas"] = {"sbx-demo-snapshots-cap": 192}

MAX_HISTORY_LEN = 3600
_k8s_clients: Optional[Tuple[Any, Any, Any]] = None
_cached_cluster_name: Optional[str] = None
_session_template_cache: Optional[Dict[str, Any]] = None

# ---------------------------------------------------------------------------
# Helper Parsing Functions (Modular & Unit-Testable)
# ---------------------------------------------------------------------------


def get_runtime_config(runtime: Optional[str] = None) -> Dict[str, Any]:
    """Returns resource names and labels for the active mode and sandbox runtime."""
    if DEMO_MODE == "snapshots-rootfs":
        # Same class, buffers and quotas as stateless gVisor: the ladder is identical.
        return {
            "runtime": "gvisor",
            "label": "gVisor + Pod Snapshots (rootfs-only)",
            "computeClass": "agent-sandbox-demo-gvisor",
            "podLabel": "agent-session",
            "warmPool": None,
            "bufferTemplate": "agent-gvisor-buffer-template",
            "standbyTemplates": {fam: f"agent-gvisor-buffer-{fam}" for fam in DEFAULT_FAMILIES},
            "families": list(DEFAULT_FAMILIES),
            "groupBy": "family",
        }
    if DEMO_MODE == "snapshots":
        return {
            "runtime": "gvisor",
            "label": "gVisor + Pod Snapshots",
            "computeClass": "agent-sandbox-demo-snapshots",
            "podLabel": "agent-session",
            "warmPool": None,
            "bufferTemplate": "agent-snapshots-buffer-template",
            "standbyTemplates": {"n4": "agent-snapshots-buffer-n4"},
            "families": list(SNAPSHOT_TYPES),
            "groupBy": "instanceType",
        }
    rt = (runtime or demo_state.get("sandboxRuntime") or "microvm").strip().lower()
    if rt != "gvisor":
        rt = "microvm"
    return {
        "runtime": rt,
        "label": "gVisor (runsc)" if rt == "gvisor" else "microVM (Kata)",
        "computeClass": f"agent-sandbox-demo-{rt}",
        "podLabel": f"agent-{rt}-sandbox",
        "warmPool": f"agent-{rt}-warmpool",
        "bufferTemplate": f"agent-{rt}-buffer-template",
        "standbyTemplates": {fam: f"agent-{rt}-buffer-{fam}" for fam in DEFAULT_FAMILIES},
        "families": list(DEFAULT_FAMILIES),
        "groupBy": "family",
    }


def agent_prefix(runtime: str) -> str:
    """Name prefix of the tower's objects: claims per runtime, or session Sandboxes."""
    return "session-" if SESSIONS else f"agent-{runtime}-"


def plan_indexed(
    existing: List[str], prefix: str, target: int, max_creates: int = MAX_CREATES_PER_PASS
) -> Tuple[List[str], List[str]]:
    """Plans creates and deletes so that exactly <prefix>0000 .. <prefix>{target-1}
    exist. Creates are capped per pass, lowest index first; deletes come from the top,
    so a scale-down removes the newest agents."""
    want = {f"{prefix}{i:04d}" for i in range(max(0, int(target)))}
    have = set(existing)
    create = sorted(want - have)[: max(0, max_creates)]
    delete = sorted(have - want, reverse=True)
    return create, delete


def _k8s_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _condition(obj: Dict[str, Any], ctype: str) -> Optional[Dict[str, Any]]:
    for cond in obj.get("status", {}).get("conditions", []) or []:
        if cond.get("type") == ctype:
            return cond
    return None


def is_pod_ready(pod: Dict[str, Any]) -> bool:
    cond = _condition(pod, "Ready")
    return bool(cond and cond.get("status") == "True")


def summarize_sandbox_pods(pods: List[Dict[str, Any]]) -> Dict[str, int]:
    """Splits sandbox pods into warm (still in the pool) and claimed, ready or not."""
    out = {"warmReady": 0, "warmPending": 0, "claimedReady": 0, "claimedPending": 0}
    for p in pods:
        meta = p.get("metadata", {})
        if meta.get("deletionTimestamp"):
            continue
        warm = WARM_POOL_LABEL in (meta.get("labels") or {})
        ready = is_pod_ready(p)
        key = ("warm" if warm else "claimed") + ("Ready" if ready else "Pending")
        out[key] += 1
    return out


def _latency_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"avg": 0.0, "p50": 0.0, "p95": 0.0}
    s = sorted(values)
    return {
        "avg": round(sum(s) / len(s), 2),
        "p50": round(s[int(len(s) * 0.5)], 2),
        "p95": round(s[min(len(s) - 1, int(len(s) * 0.95))], 2),
    }


def compute_claim_latencies(
    claims: List[Dict[str, Any]],
    pods: List[Dict[str, Any]],
    min_created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Claim-to-Ready time, and how many claims the warm pool served. A claim counts as
    a warm hit when its pod existed before the claim did."""
    pod_created = {
        p.get("metadata", {}).get("name", ""): _k8s_ts(p.get("metadata", {}).get("creationTimestamp"))
        for p in pods
    }
    min_ts = min_created_at.timestamp() if min_created_at else None
    latencies: List[float] = []
    warm_hits = 0
    for c in claims:
        created = _k8s_ts(c.get("metadata", {}).get("creationTimestamp"))
        if created is None or (min_ts is not None and created < min_ts):
            continue
        ready = _condition(c, "Ready")
        if not ready or ready.get("status") != "True":
            continue
        ready_at = _k8s_ts(ready.get("lastTransitionTime"))
        if ready_at is None:
            continue
        latencies.append(max(0.0, ready_at - created))
        sbx_name = (c.get("status", {}).get("sandbox") or {}).get("name") or (
            c.get("metadata", {}).get("annotations") or {}
        ).get("agents.x-k8s.io/sandbox-name", "")
        pod_at = pod_created.get(sbx_name)
        if pod_at is not None and pod_at < created:
            warm_hits += 1
    stats = _latency_stats(latencies)
    stats["count"] = len(latencies)
    stats["warmHits"] = warm_hits
    return stats


def classify_sessions(
    sandboxes: List[Dict[str, Any]],
    pods: List[Dict[str, Any]],
    ops: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Counts session Sandboxes by state: running (pod Ready), starting, snapshotting,
    suspended, or error (the snapshot before a suspend failed)."""
    ops = ops or {}
    pods_by_name = {p.get("metadata", {}).get("name", ""): p for p in pods}
    counts = {"running": 0, "starting": 0, "snapshotting": 0, "suspended": 0, "error": 0}
    states: Dict[str, str] = {}
    for s in sandboxes:
        meta = s.get("metadata", {})
        if meta.get("deletionTimestamp"):
            continue
        name = meta.get("name", "")
        op = ops.get(name, {}).get("state")
        mode = s.get("spec", {}).get("operatingMode") or "Running"
        pod = pods_by_name.get(name)
        if op in ("snapshotting", "suspending"):
            state = "snapshotting"
        elif mode == "Suspended":
            state = "suspended"
        elif op == "error":
            state = "error"
        elif pod is not None and is_pod_ready(pod):
            state = "running"
        else:
            state = "starting"
        counts[state] += 1
        states[name] = state
    counts["total"] = sum(counts.values())
    return {"counts": counts, "states": states}


def record_resume_latencies(
    pods: List[Dict[str, Any]],
    resume_started: Dict[str, float],
    sink: List[float],
) -> None:
    """Moves sessions whose pod became Ready after a resume into the latency sink."""
    for p in pods:
        name = p.get("metadata", {}).get("name", "")
        started = resume_started.get(name)
        if started is None or not is_pod_ready(p):
            continue
        ready_at = _k8s_ts((_condition(p, "Ready") or {}).get("lastTransitionTime"))
        if ready_at is None or ready_at < started - 1:
            continue
        sink.append(max(0.0, ready_at - started))
        del resume_started[name]
    del sink[:-MAX_LATENCY_SAMPLES]


def parse_session_state(log_text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Reads the last JSON line a rootfs-mode session container logged, or None.
    The line carries file state (boots, firstBootAt, fileWrites: kept on the root
    filesystem) and process state (processStart, memTicks: in memory only)."""
    for line in reversed((log_text or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "processStart" in obj and "firstBootAt" in obj:
            return obj
    return None


def compare_session_state(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]
) -> Optional[Dict[str, bool]]:
    """What a resume restored, from the state logged before the snapshot and after the
    resume. File state survived if the same state file is still there and did not go
    back in time; process state survived only if it is the same process (same start)."""
    if not before or not after:
        return None
    return {
        "fileKept": after.get("firstBootAt") == before.get("firstBootAt")
        and (after.get("fileWrites") or 0) >= (before.get("fileWrites") or 0),
        "memoryKept": after.get("processStart") == before.get("processStart"),
    }


def summarize_survival(results: Dict[str, Dict[str, bool]], pending: int = 0) -> Dict[str, Any]:
    """Aggregates per-session resume results for the dashboard."""
    return {
        "checked": len(results),
        "pending": pending,
        "fileKept": sum(1 for r in results.values() if r.get("fileKept")),
        "memoryKept": sum(1 for r in results.values() if r.get("memoryKept")),
    }


def mock_survival(target_agents: int) -> Dict[str, Any]:
    """Simulated rootfs-only outcome for the offline mock: files kept, memory lost."""
    return {
        "checked": target_agents, "pending": 0,
        "fileKept": target_agents, "memoryKept": 0, "simulated": True,
    }


def node_cores(node: Dict[str, Any], default: int = 8) -> int:
    """vCPUs of a node, from the instance type suffix (n4-standard-16 -> 16)."""
    labels = node.get("metadata", {}).get("labels", {})
    itype = labels.get("node.kubernetes.io/instance-type") or labels.get(
        "beta.kubernetes.io/instance-type"
    ) or ""
    m = CORES_SUFFIX_REGEX.search(itype.strip().lower())
    return int(m.group(1)) if m else default


def node_group_key(node: Dict[str, Any], group_by: str = "family") -> str:
    """Groups nodes by machine family, or by instance type in snapshots mode, where every
    rule is the same family and only the size changes."""
    if group_by == "instanceType":
        labels = node.get("metadata", {}).get("labels", {})
        itype = labels.get("node.kubernetes.io/instance-type") or labels.get(
            "beta.kubernetes.io/instance-type"
        )
        if itype:
            return str(itype).strip().lower()
    return extract_machine_family(node)


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



def capacity_warmup(buffer_status: Dict[str, Any], mock: bool = False) -> Dict[str, Any]:
    """Whether the standing capacity is built: every CapacityBuffer is ReadyForProvisioning
    and the standby nodes have been created and suspended. Scaling earlier works, but it
    measures cold node starts instead of the warm layer."""
    if mock:
        return {"ready": True, "detail": "mock"}
    total = int(buffer_status.get("totalBuffers", 0))
    ready = int(buffer_status.get("readyBuffers", 0))
    want = int(buffer_status.get("standbyReplicas", 0))
    susp = int(buffer_status.get("suspendedNodes", 0))
    ok = total > 0 and ready >= total and susp >= want
    return {
        "ready": ok,
        "detail": f"buffers {ready}/{total} ready, standby nodes {susp}/{want} suspended",
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
    now: Optional[datetime] = None,
    buffers: Optional[List[Dict[str, Any]]] = None,
    cluster_name: str = "mock-simulation",
    sandbox_runtime: str = "microvm",
    claims: Optional[List[Dict[str, Any]]] = None,
    sandboxes: Optional[List[Dict[str, Any]]] = None,
    warm_pool_replicas: Optional[int] = None,
    survival: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assembles all telemetry into the unified dashboard payload."""
    if now is None:
        now = datetime.now(timezone.utc)

    rt_cfg = get_runtime_config(sandbox_runtime)
    group_by = rt_cfg["groupBy"]

    # 1. Parse ComputeClass first to discover dynamic priority tiers & node groups
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
        if group_by == "instanceType" and p.get("machineType") and not p[
            "machineType"
        ].endswith("-family"):
            p["family"] = p["machineType"]
        pfam = p.get("family") or (
            rt_cfg["families"][idx]
            if idx < len(rt_cfg["families"])
            else f"tier{idx}"
        )
        p["family"] = pfam
        if not p.get("name") or p["name"] == f"Tier {p['identifier']}":
            p["name"] = f"Tier {p['identifier']}: {pfam}"
        if pfam not in discovered_families:
            discovered_families.append(pfam)

    if not discovered_families:
        discovered_families = list(rt_cfg["families"])

    # 2. Node aggregation by group (family or instance type), priority annotation, and
    # standby suspension. Suspended standby nodes are not counted as active capacity.
    nodes_by_family: Dict[str, int] = {fam: 0 for fam in discovered_families}
    nodes_by_family["other"] = 0
    cpu_by_group: Dict[str, int] = {}
    cpu_by_machine_family: Dict[str, int] = {}
    suspended_by_family: Dict[str, int] = {fam: 0 for fam in discovered_families}
    nodes_by_annotation: Dict[str, int] = {
        str(i): 0 for i in range(max(5, len(cc_status.get("priorities", []))))
    }
    nodes_by_annotation["ccc_scale_up_anyway"] = 0
    nodes_by_annotation["pending_annotation"] = 0
    nodes_by_annotation["unannotated"] = 0

    total_ready_nodes = 0
    suspended_nodes = 0
    total_cpu = 0

    for n in nodes:
        fam = node_group_key(n, group_by)
        if is_node_suspended(n):
            suspended_nodes += 1
            if fam and fam != "unknown":
                suspended_by_family[fam] = suspended_by_family.get(fam, 0) + 1
            continue

        cores = node_cores(n)
        total_cpu += cores
        mfam = extract_machine_family(n)
        cpu_by_machine_family[mfam] = cpu_by_machine_family.get(mfam, 0) + cores
        if fam and fam != "unknown":
            if fam not in nodes_by_family:
                nodes_by_family[fam] = 0
                if fam not in discovered_families:
                    discovered_families.append(fam)
            nodes_by_family[fam] += 1
            cpu_by_group[fam] = cpu_by_group.get(fam, 0) + cores
        else:
            nodes_by_family["other"] += 1

        ann = extract_priority_index(n, now)
        if ann in nodes_by_annotation:
            nodes_by_annotation[ann] += 1
        else:
            nodes_by_annotation["unannotated"] += 1

        for cond in n.get("status", {}).get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                total_ready_nodes += 1
                break

    total_nodes = max(0, len(nodes) - suspended_nodes)

    # 3. Agents. Stateless: one agent per claimed sandbox, warm-pool pods counted apart.
    # Snapshots: one agent per session Sandbox.
    pod_counts = summarize_sandbox_pods(pods)
    sessions = None
    if SESSIONS:
        sessions = classify_sessions(
            sandboxes or [], pods, demo_state.get("sessionOps")
        )["counts"]
        ready_sandboxes = sessions["running"]
        pending_sandboxes = sessions["starting"]
    else:
        ready_sandboxes = pod_counts["claimedReady"]
        pending_sandboxes = pod_counts["claimedPending"]
    active_agents = ready_sandboxes
    if warm_pool_replicas is None:
        warm_pool_replicas = demo_state.get("warmPoolReplicas", 0)

    consolidation_delay_mins = cc_status.get("topLevel", {}).get(
        "consolidationDelayMinutes",
        demo_state.get("consolidationDelayMinutes", 1),
    )
    in_progress_total = sum(
        p.get("inProgressNodes", 0) for p in cc_status.get("priorities", [])
    )

    # 4. Latency
    reset_at = demo_state.get("metrics_reset_at")
    latencies = compute_pod_latencies(pods, now, min_created_at=reset_at)
    latencies["sandboxBoot"] = latencies["microvmBoot"]
    since_dt = reset_at or demo_state.get("started_at") or now
    latencies["metricsSince"] = since_dt.strftime("%H:%M:%S UTC")
    if SESSIONS:
        latencies["resume"] = _latency_stats(demo_state.get("resumeLatencies", []))
        latencies["resume"]["count"] = len(demo_state.get("resumeLatencies", []))
        latencies["snapshot"] = _latency_stats(demo_state.get("snapshotLatencies", []))
        latencies["snapshot"]["count"] = len(demo_state.get("snapshotLatencies", []))
    else:
        latencies["claimReady"] = compute_claim_latencies(
            claims or [], pods, min_created_at=reset_at
        )

    # 5. CapacityQuotas (family & class-wide matching), from node vCPUs
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
            target_fams = [str(q_fam).strip().lower()]
            scope = "family"
        elif expr_fams:
            target_fams = expr_fams
            scope = "family"
        else:
            target_fams = []
            scope = "class"
        if scope == "family":
            used_cpu = sum(cpu_by_machine_family.get(f, 0) for f in target_fams)
        else:
            used_cpu = total_cpu

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
        fam_used_cpu = cpu_by_group.get(pfam, nodes_by_family.get(pfam, 0) * cores_per_node)
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
        families=list(rt_cfg["standbyTemplates"]),
    )

    # 7. Rule-0 vs. fallback share
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
            "mode": DEMO_MODE,
            "clusterName": cluster_name,
            "sandboxRuntime": rt_cfg["runtime"],
            "sandboxRuntimeLabel": rt_cfg["label"],
            "computeClassName": rt_cfg["computeClass"],
            "targetAgents": target_agents,
            "activeAgents": active_agents,
            # One agent per sandbox. Kept for the shared UI's agents = sandboxes x ratio.
            "actorsPerWorker": 1,
            "targetSandboxes": target_agents,
            "readySandboxes": ready_sandboxes,
            "pendingSandboxes": pending_sandboxes,
            "warmPool": {
                "name": rt_cfg["warmPool"],
                "target": warm_pool_replicas if rt_cfg["warmPool"] else 0,
                "ready": pod_counts["warmReady"],
                "pending": pod_counts["warmPending"],
            },
            "sessions": sessions,
            "resumeSurvival": survival,
            "totalNodes": total_nodes,
            "totalCpu": total_cpu,
            "readyNodes": total_ready_nodes,
            "capacityWarmup": capacity_warmup(buffer_status, bool(demo_state.get("mock_mode"))),
            "suspendedNodes": suspended_nodes,
            "suspendedByFamily": suspended_by_family,
            "inProgressNodes": in_progress_total,
            "consolidationDelayMinutes": consolidation_delay_mins,
            "rule0Share": rule0_share,
            "fallbackShare": fallback_share,
            "groupBy": group_by,
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


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _mock_quota_objects(quotas: Dict[str, int], class_names: List[str]) -> List[Dict[str, Any]]:
    """CapacityQuota objects shaped like manifests/02 and snapshots/02."""
    out = []
    for name, cpu in quotas.items():
        m = re.match(r"^sbx-demo-([a-z]+\d+[a-z]*)-cap$", name)
        selector: Dict[str, Any] = {
            "matchExpressions": [
                {
                    "key": "cloud.google.com/compute-class",
                    "operator": "In",
                    "values": class_names,
                }
            ]
        }
        if m:
            selector["matchLabels"] = {"cloud.google.com/machine-family": m.group(1)}
        out.append(
            {
                "metadata": {"name": name},
                "spec": {"selector": selector, "limits": {"resources": {"cpu": cpu}}},
            }
        )
    return out


def generate_mock_cluster_data(
    target_agents: int,
    quotas: Dict[str, int],
    now: Optional[datetime] = None,
    sandbox_runtime: Optional[str] = None,
    warm_pool_replicas: Optional[int] = None,
    suspended: Optional[bool] = None,
) -> Dict[str, Any]:
    """Simulates node provisioning down the class's ladder, quota caps, and agents."""
    if now is None:
        now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    rt_cfg = get_runtime_config(sandbox_runtime)
    snapshots = SESSIONS
    one_family = ONE_FAMILY
    if warm_pool_replicas is None:
        warm_pool_replicas = demo_state.get("warmPoolReplicas", 0)
    if suspended is None:
        suspended = demo_state.get("mockSuspended", False)
    target_agents = max(0, int(target_agents))

    # (machine type, family, cores, sandboxes per node, node limit from quota or a
    # simulated stockout; None means unlimited)
    if snapshots:
        warm_pool_replicas = 0
    if one_family:
        class_cap = quotas.get("sbx-demo-snapshots-cap", 192)
        ladder = [
            ("n4-standard-8", "n4", 8, 12, 6),
            ("n4-standard-16", "n4", 16, 26, None),
            ("n4-standard-4", "n4", 4, 5, None),
        ]
        class_names = [rt_cfg["computeClass"]]
    else:
        # microVM adds 250m of pod overhead, so fewer fit per node than gVisor.
        per_node = 14 if rt_cfg["runtime"] == "microvm" else 22
        class_cap = quotas.get("sbx-demo-class-cap", 720)
        ladder = [
            (f"{fam}-standard-8", fam, 8, per_node,
             quotas[f"sbx-demo-{fam}-cap"] // 8 if f"sbx-demo-{fam}-cap" in quotas else None)
            for fam in DEFAULT_FAMILIES
        ]
        class_names = (
            [rt_cfg["computeClass"]] if snapshots
            else ["agent-sandbox-demo-microvm", "agent-sandbox-demo-gvisor"]
        )

    running_agents = 0 if (snapshots and suspended) else target_agents
    demand = running_agents + warm_pool_replicas
    remaining = demand
    cpu_left = class_cap
    placed_nodes: List[Tuple[str, str, int, int]] = []  # (type, family, cores, count)
    filled_rules: List[bool] = []
    for mtype, fam, cores, per, limit in ladder:
        want = (remaining + per - 1) // per if remaining > 0 else 0
        count = min(want, cpu_left // cores)
        if limit is not None:
            count = min(count, limit)
        placed_nodes.append((mtype, fam, cores, count))
        filled_rules.append(limit is not None and count == limit and want > limit)
        remaining = max(0, remaining - count * per)
        cpu_left -= count * cores
    placed_slots = demand - remaining

    simulated_nodes = []
    for idx, (mtype, fam, cores, count) in enumerate(placed_nodes):
        for n in range(count):
            simulated_nodes.append(
                {
                    "metadata": {
                        "name": f"gke-sbx-demo-{mtype}-{n:04d}",
                        "creationTimestamp": now.isoformat(),
                        "labels": {
                            "cloud.google.com/machine-family": fam,
                            "node.kubernetes.io/instance-type": mtype,
                            "cloud.google.com/compute-class": rt_cfg["computeClass"],
                        },
                        "annotations": {"cloud.google.com/ccc_priority_index": str(idx)},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            )

    def _pod(name: str, placed: bool, created: float, labels: Dict[str, str]) -> Dict[str, Any]:
        conds = []
        if placed:
            conds = [
                {"type": "PodScheduled", "status": "True", "lastTransitionTime": _iso(created + 2)},
                {"type": "ContainersReady", "status": "True", "lastTransitionTime": _iso(created + 4)},
                {"type": "Ready", "status": "True", "lastTransitionTime": _iso(created + 4)},
            ]
        return {
            "metadata": {"name": name, "creationTimestamp": _iso(created), "labels": labels},
            "status": {"phase": "Running" if placed else "Pending", "conditions": conds},
        }

    simulated_pods: List[Dict[str, Any]] = []
    claims: List[Dict[str, Any]] = []
    sandboxes: List[Dict[str, Any]] = []
    prefix = agent_prefix(rt_cfg["runtime"])
    # Claimed sandboxes are placed before the warm pool refills.
    for i in range(target_agents):
        name = f"{prefix}{i:04d}"
        if snapshots:
            sandboxes.append(
                {
                    "metadata": {"name": name},
                    "spec": {"operatingMode": "Suspended" if suspended else "Running"},
                }
            )
            if not suspended:
                simulated_pods.append(
                    _pod(name, i < placed_slots, now_ts - 30, {"app": rt_cfg["podLabel"]})
                )
            continue
        warm_hit = i < warm_pool_replicas
        claim_at = now_ts - 60
        pod_name = f"{rt_cfg['warmPool']}-{i:05d}"
        simulated_pods.append(
            _pod(pod_name, i < placed_slots, claim_at - (60 if warm_hit else 0), {"app": rt_cfg["podLabel"]})
        )
        if i < placed_slots:
            claims.append(
                {
                    "metadata": {"name": name, "creationTimestamp": _iso(claim_at)},
                    "status": {
                        "sandbox": {"name": pod_name},
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "True",
                                "lastTransitionTime": _iso(claim_at + (1.2 if warm_hit else 45)),
                            }
                        ],
                    },
                }
            )
    for i in range(warm_pool_replicas):
        simulated_pods.append(
            _pod(
                f"{rt_cfg['warmPool']}-warm-{i:04d}",
                running_agents + i < placed_slots,
                now_ts - 120,
                {"app": rt_cfg["podLabel"], WARM_POOL_LABEL: "mock"},
            )
        )

    backoff = datetime.fromtimestamp(now_ts + 600, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    priority_statuses = []
    for idx, (mtype, fam, cores, count) in enumerate(placed_nodes):
        conds = []
        if filled_rules[idx] and idx == 0 and not one_family:
            conds = [
                {
                    "type": "ProvisioningSuspended",
                    "reason": "QuotaExceeded",
                    "message": f"NodeProvisioning associated with this priority failed due to QuotaExceeded ({fam.upper()}_CPUS). Backing off the priority until {backoff} UTC.",
                }
            ]
        elif filled_rules[idx] and one_family:
            conds = [
                {
                    "type": "ProvisioningConstrained",
                    "reason": "OutOfResources",
                    "message": f"NodeProvisioning associated with this priority is constrained (2/3) of configurations are excluded due to OutOfResources. Backing off until {backoff} UTC.",
                }
            ]
        priority_statuses.append(
            {
                "identifier": str(idx),
                "conditions": conds,
                "resourceInfo": [
                    {
                        "name": "cpu",
                        "currentCount": count * cores,
                        "targetCount": count * cores,
                        "currentUtilizationPercentage": 100.0 if count else 0.0,
                    }
                ],
                "scalingEventsHistory": {
                    "provisionedNodesCount": count,
                    "consolidatedNodesCount": 0,
                    "migratedNodesCount": 0,
                },
            }
        )
    rules = [{"machineType": mtype, "maxPodsPerNode": 32} for mtype, *_ in ladder]
    if not one_family:
        rules[-1] = {"machineFamily": "n2", "minCores": 8, "minMemoryGb": 32, "maxPodsPerNode": 32}
    cc_mock = {
        "metadata": {"name": rt_cfg["computeClass"]},
        "spec": {
            "autoscalingPolicy": {
                "consolidationDelayMinutes": demo_state.get("consolidationDelayMinutes", 1)
            },
            "priorities": rules,
        },
        "status": {
            "conditions": [{"type": "Health", "status": "True", "message": "Crd is healthy."}],
            "priorityStatuses": priority_statuses,
        },
    }

    standby_fams = list(rt_cfg["standbyTemplates"])
    standby_dist = distribute_standby_replicas(
        demo_state.get("standbyReplicas", 6), standby_fams
    )
    mock_buffers = [
        {
            "metadata": {"name": "agent-active-buffer", "labels": {"buffer-strategy": "active"}},
            "spec": {
                "replicas": demo_state.get("activeBufferReplicas", 2),
                "provisioningStrategy": "buffer.x-k8s.io/active-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        }
    ] + [
        {
            "metadata": {"name": f"agent-standby-buffer-{fam}", "labels": {"machine-family": fam}},
            "spec": {
                "replicas": standby_dist[fam],
                "provisioningStrategy": "buffer.gke.io/standby-capacity",
            },
            "status": {"status": "ReadyForProvisioning"},
        }
        for fam in standby_fams
    ]

    return assemble_telemetry(
        nodes=simulated_nodes,
        pods=simulated_pods,
        cc_obj=cc_mock,
        quotas=_mock_quota_objects(quotas, class_names),
        buffers=mock_buffers,
        target_agents=target_agents,
        now=now,
        sandbox_runtime=rt_cfg["runtime"],
        claims=claims,
        sandboxes=sandboxes,
        warm_pool_replicas=warm_pool_replicas,
        survival=(
            mock_survival(target_agents)
            if DEMO_MODE == "snapshots-rootfs" and demo_state.get("mockResumed")
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Live Kubernetes Collector
# ---------------------------------------------------------------------------


_k8s_executor = ThreadPoolExecutor(max_workers=8)
# Claim/session creates and deletes, and snapshot-then-suspend operations, which poll.
_ops_executor = ThreadPoolExecutor(max_workers=16)


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


def _list_ns(custom_api: Any, group: str, version: str, plural: str, selector: str = "") -> List[Dict[str, Any]]:
    return custom_api.list_namespaced_custom_object(
        group=group,
        version=version,
        namespace=DEMO_NAMESPACE,
        plural=plural,
        label_selector=selector,
    ).get("items", [])


def _list_claims(custom_api: Any, runtime: Optional[str] = None) -> List[Dict[str, Any]]:
    sel = f"{PART_OF_LABEL}={PART_OF}"
    if runtime:
        sel += f",{RUNTIME_LABEL}={runtime}"
    return _list_ns(custom_api, EXT_GROUP, SBX_VERSION, "sandboxclaims", sel)


def _list_sessions(custom_api: Any) -> List[Dict[str, Any]]:
    return _list_ns(custom_api, AGENTS_GROUP, SBX_VERSION, "sandboxes", f"{ROLE_LABEL}=session")


def _list_pods(core_v1: Any, app_label: str) -> List[Dict[str, Any]]:
    raw = core_v1.list_namespaced_pod(
        namespace=DEMO_NAMESPACE,
        label_selector=f"app={app_label}",
        _preload_content=False,
    )
    return json.loads(raw.data).get("items", [])


def _read_session_state(core_v1: Any, name: str) -> Optional[Dict[str, Any]]:
    """Last state line from a session pod's log (needs pods/log get), or None."""
    try:
        text = core_v1.read_namespaced_pod_log(
            name=name, namespace=DEMO_NAMESPACE, container=SESSION_CONTAINER, tail_lines=5,
        )
    except Exception as e:
        logger.debug(f"Session log read for {name} failed: {e}")
        return None
    return parse_session_state(text)


def _probe_survival(core_v1: Any, name: str) -> None:
    after = _read_session_state(core_v1, name)
    result = compare_session_state(demo_state["preSuspend"].get(name), after)
    if after is None:
        return  # not logging yet; retried on the next poll
    if result is not None:
        demo_state["survival"][name] = result
    demo_state["pendingSurvival"].pop(name, None)


def collect_survival(core_v1: Any, pods: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rootfs mode: for each resumed session whose pod is Ready, reads its state and compares
    it with the pre-suspend state. Probes run in the background; this returns what is known."""
    pending = demo_state["pendingSurvival"]
    now = time.time()
    ready = {p.get("metadata", {}).get("name") for p in pods if is_pod_ready(p)}
    for name, started in list(pending.items()):
        if now - started > SURVIVAL_TIMEOUT_SECONDS:
            pending.pop(name, None)  # never logged: leave it unchecked rather than guess
        elif name in ready:
            _ops_executor.submit(_probe_survival, core_v1, name)
    return summarize_survival(dict(demo_state["survival"]), len(pending))


def get_live_telemetry() -> Optional[Dict[str, Any]]:
    """Queries the live Kubernetes cluster concurrently."""
    try:
        rt_cfg = get_runtime_config(demo_state.get("sandboxRuntime", "microvm"))
        core_v1, _, custom_api = _get_k8s_clients()

        def _fetch_nodes():
            raw = core_v1.list_node(
                label_selector=f"cloud.google.com/compute-class={rt_cfg['computeClass']}",
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
                    group=BUFFER_GROUP,
                    version=BUFFER_VERSION,
                    plural="capacityquotas",
                    label_selector=f"{PART_OF_LABEL}={PART_OF}",
                )
                names = demo_state["quotas"]
                return [q for q in res.get("items", []) if q["metadata"]["name"] in names]
            except Exception as e:
                logger.debug(f"CapacityQuota lookup failed: {e}")
                return []

        def _fetch_buffers():
            try:
                items = _list_ns(custom_api, BUFFER_GROUP, BUFFER_VERSION, "capacitybuffers")
            except Exception as e:
                logger.debug(f"CapacityBuffer list failed: {e}")
                return []
            # The stateless buffers stay installed in snapshots mode, zeroed; hide them.
            keep = {"agent-active-buffer"} | {
                f"agent-standby-buffer-{fam}" for fam in rt_cfg["standbyTemplates"]
            }
            return [b for b in items if b["metadata"]["name"] in keep]

        def _fetch_agents():
            try:
                if SESSIONS:
                    return _list_sessions(custom_api)
                return _list_claims(custom_api, rt_cfg["runtime"])
            except Exception as e:
                logger.debug(f"Agent list failed: {e}")
                return []

        futures = {
            "nodes": _k8s_executor.submit(_fetch_nodes),
            "pods": _k8s_executor.submit(_list_pods, core_v1, rt_cfg["podLabel"]),
            "cc": _k8s_executor.submit(_fetch_cc),
            "quotas": _k8s_executor.submit(_fetch_quotas),
            "buffers": _k8s_executor.submit(_fetch_buffers),
            "agents": _k8s_executor.submit(_fetch_agents),
        }
        res = {k: f.result() for k, f in futures.items()}

        if SESSIONS:
            record_resume_latencies(
                res["pods"], demo_state["resumeStartedAt"], demo_state["resumeLatencies"]
            )

        survival = None
        if DEMO_MODE == "snapshots-rootfs":
            survival = collect_survival(core_v1, res["pods"])

        return assemble_telemetry(
            nodes=res["nodes"],
            pods=res["pods"],
            cc_obj=res["cc"],
            quotas=res["quotas"],
            buffers=res["buffers"],
            target_agents=demo_state["targetAgents"],
            cluster_name=_detect_cluster_name(),
            sandbox_runtime=rt_cfg["runtime"],
            claims=res["agents"] if not SESSIONS else None,
            sandboxes=res["agents"] if SESSIONS else None,
            survival=survival,
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
        quotas=demo_state["quotas"],
        sandbox_runtime=demo_state.get("sandboxRuntime", "microvm"),
    )


async def get_cached_or_fresh_telemetry() -> Dict[str, Any]:
    """Returns background-cached telemetry if fresh (<2.5s), else refreshes off-thread.
    Every state change clears the cache, so a cached payload always matches the state."""
    cached = demo_state.get("latest_telemetry")
    cached_ts = demo_state.get("latest_telemetry_ts", 0.0)
    if cached is not None and (time.monotonic() - cached_ts) < 2.5:
        return cached

    ver_before = demo_state.get("state_version", 0)
    data = await asyncio.to_thread(get_current_telemetry)
    if demo_state.get("state_version", 0) == ver_before:
        demo_state["latest_telemetry"] = data
        demo_state["latest_telemetry_ts"] = time.monotonic()
    return data


# ---------------------------------------------------------------------------
# Reconcilers: SandboxClaims (stateless) and session Sandboxes (snapshots)
# ---------------------------------------------------------------------------


def _patch_once(custom_api: Any, plural: str, name: str, body: Dict[str, Any], force: bool = False) -> None:
    """Patches a namespaced warm pool or buffer, skipping a patch identical to the last."""
    key = f"{plural}/{name}"
    sig = json.dumps(body, sort_keys=True)
    if not force and demo_state["patched"].get(key) == sig:
        return
    group, version = (
        (BUFFER_GROUP, BUFFER_VERSION) if plural == "capacitybuffers" else (EXT_GROUP, SBX_VERSION)
    )
    try:
        custom_api.patch_namespaced_custom_object(
            group=group, version=version, namespace=DEMO_NAMESPACE,
            plural=plural, name=name, body=body,
        )
        demo_state["patched"][key] = sig
    except Exception as e:
        # A snapshots-only install has no standby buffers for the other families.
        if getattr(e, "status", None) == 404:
            demo_state["patched"][key] = sig
            logger.debug(f"{key} not found; skipped")
        else:
            logger.warning(f"Failed to patch {key}: {e}")


def apply_buffers(custom_api: Any, force: bool = False) -> Dict[str, int]:
    """Points the CapacityBuffers at the active runtime's (or the snapshot class's)
    PodTemplates and sets their replicas. Standby buffers the mode doesn't use go to 0."""
    rt_cfg = get_runtime_config()
    _patch_once(
        custom_api,
        "capacitybuffers",
        "agent-active-buffer",
        {
            "spec": {
                "podTemplateRef": {"name": rt_cfg["bufferTemplate"]},
                "replicas": demo_state["activeBufferReplicas"],
            }
        },
        force,
    )
    dist = distribute_standby_replicas(
        demo_state["standbyReplicas"], list(rt_cfg["standbyTemplates"])
    )
    for fam in DEFAULT_FAMILIES:
        spec: Dict[str, Any] = {"replicas": dist.get(fam, 0)}
        if fam in rt_cfg["standbyTemplates"]:
            spec["podTemplateRef"] = {"name": rt_cfg["standbyTemplates"][fam]}
        _k8s_executor.submit(
            _patch_once, custom_api, "capacitybuffers", f"agent-standby-buffer-{fam}", {"spec": spec}, force
        )
    return dist


# Serializes adoption against /api/scale, so a target set while the first reconcile is
# still reading the cluster is not overwritten by what the cluster held before.
_adopt_lock = threading.Lock()


def _mark_adopted() -> None:
    with _adopt_lock:
        demo_state["adopted"] = True


def _adopt_cluster_target(custom_api: Any) -> None:
    """On the first reconcile after a (re)start, takes the target, runtime, warm pool and
    buffer sizes from the cluster, so a tower restart never scales running agents to zero."""
    if SESSIONS:
        demo_state["targetAgents"] = sum(
            1 for s in _list_sessions(custom_api) if not s["metadata"].get("deletionTimestamp")
        )
    else:
        totals = {"microvm": 0, "gvisor": 0}
        for c in _list_claims(custom_api):
            rt = c["metadata"].get("labels", {}).get(RUNTIME_LABEL)
            if rt in totals and not c["metadata"].get("deletionTimestamp"):
                totals[rt] += 1
        rt = max(totals, key=totals.get)
        if totals[rt] > 0:
            demo_state["sandboxRuntime"] = rt
        demo_state["targetAgents"] = totals[rt]
        try:
            wp = custom_api.get_namespaced_custom_object(
                group=EXT_GROUP, version=SBX_VERSION, namespace=DEMO_NAMESPACE,
                plural="sandboxwarmpools", name=get_runtime_config()["warmPool"],
            )
            replicas = int(wp.get("spec", {}).get("replicas") or 0)
            if replicas > 0:
                demo_state["warmPoolReplicas"] = replicas
        except Exception as e:
            logger.debug(f"Warm pool lookup failed: {e}")
    try:
        bufs = {
            b["metadata"]["name"]: int(b.get("spec", {}).get("replicas") or 0)
            for b in _list_ns(custom_api, BUFFER_GROUP, BUFFER_VERSION, "capacitybuffers")
        }
        if "agent-active-buffer" in bufs:
            demo_state["activeBufferReplicas"] = bufs["agent-active-buffer"]
        standby = [
            v for k, v in bufs.items()
            if k.startswith("agent-standby-buffer-")
            and k.rsplit("-", 1)[-1] in get_runtime_config()["standbyTemplates"]
        ]
        if standby:
            demo_state["standbyReplicas"] = sum(standby)
    except Exception as e:
        logger.debug(f"Buffer lookup failed: {e}")
    demo_state["adopted"] = True
    logger.info(
        f"Adopted cluster target: {demo_state['targetAgents']} agents "
        f"({DEMO_MODE}, {demo_state['sandboxRuntime']}), warm pool {demo_state['warmPoolReplicas']}"
    )


def _run_all(fn, names: List[str]) -> None:
    for f in [_ops_executor.submit(fn, n) for n in names]:
        try:
            f.result()
        except Exception as e:
            logger.warning(f"{fn.__name__} failed: {e}")


def _get_session_template(custom_api: Any) -> Dict[str, Any]:
    global _session_template_cache
    if _session_template_cache is None:
        _session_template_cache = custom_api.get_namespaced_custom_object(
            group=EXT_GROUP, version=SBX_VERSION, namespace=DEMO_NAMESPACE,
            plural="sandboxtemplates", name=SESSION_TEMPLATE,
        )
    return _session_template_cache


def session_sandbox_body(name: str, template: Dict[str, Any]) -> Dict[str, Any]:
    """A session Sandbox: the template's podTemplate plus the `session` label that keys
    snapshot restore. The label must be on the pod from creation."""
    pod_template = copy.deepcopy(template.get("spec", {}).get("podTemplate", {}))
    pod_template.setdefault("metadata", {}).setdefault("labels", {})["session"] = name
    return {
        "apiVersion": f"{AGENTS_GROUP}/{SBX_VERSION}",
        "kind": "Sandbox",
        "metadata": {
            "name": name,
            "namespace": DEMO_NAMESPACE,
            "labels": {PART_OF_LABEL: PART_OF, ROLE_LABEL: "session"},
        },
        "spec": {"podTemplate": pod_template},
    }


def claim_body(name: str, runtime: str) -> Dict[str, Any]:
    return {
        "apiVersion": f"{EXT_GROUP}/{SBX_VERSION}",
        "kind": "SandboxClaim",
        "metadata": {
            "name": name,
            "namespace": DEMO_NAMESPACE,
            "labels": {PART_OF_LABEL: PART_OF, RUNTIME_LABEL: runtime},
        },
        "spec": {"warmPoolRef": {"name": f"agent-{runtime}-warmpool"}},
    }


def reconcile_agents(force: bool = False) -> None:
    """Drives the cluster toward the target: one SandboxClaim per agent on the active
    runtime (the other runtime's claims and warm pool go to zero), or one session Sandbox
    per agent in snapshots mode."""
    demo_state["lastReconcile"] = time.time()
    _, _, custom_api = _get_k8s_clients()
    with _adopt_lock:
        adopting = not demo_state["adopted"]
        if adopting:
            _adopt_cluster_target(custom_api)
    if adopting:
        apply_buffers(custom_api, force=True)
        force = True
    rt_cfg = get_runtime_config()
    target = demo_state["targetAgents"]

    def _delete(plural: str, group: str):
        def _do(name: str) -> None:
            try:
                custom_api.delete_namespaced_custom_object(
                    group=group, version=SBX_VERSION, namespace=DEMO_NAMESPACE,
                    plural=plural, name=name,
                )
            except Exception as e:
                if getattr(e, "status", None) != 404:
                    raise
        _do.__name__ = f"delete_{plural}"
        return _do

    if SESSIONS:
        names = [
            s["metadata"]["name"] for s in _list_sessions(custom_api)
            if not s["metadata"].get("deletionTimestamp")
        ]
        create, delete = plan_indexed(names, agent_prefix("gvisor"), target)
        if create:
            template = _get_session_template(custom_api)

            def _create_session(name: str) -> None:
                custom_api.create_namespaced_custom_object(
                    group=AGENTS_GROUP, version=SBX_VERSION, namespace=DEMO_NAMESPACE,
                    plural="sandboxes", body=session_sandbox_body(name, template),
                )
            _run_all(_create_session, create)
        for name in delete:
            demo_state["sessionOps"].pop(name, None)
            demo_state["resumeStartedAt"].pop(name, None)
            for key in ("preSuspend", "pendingSurvival", "survival"):
                demo_state[key].pop(name, None)
        _run_all(_delete("sandboxes", AGENTS_GROUP), delete)
        return

    by_rt: Dict[str, List[str]] = {"microvm": [], "gvisor": []}
    for c in _list_claims(custom_api):
        meta = c["metadata"]
        rt = meta.get("labels", {}).get(RUNTIME_LABEL)
        if rt in by_rt and not meta.get("deletionTimestamp"):
            by_rt[rt].append(meta["name"])
    active = rt_cfg["runtime"]
    create, delete = plan_indexed(by_rt[active], agent_prefix(active), target)
    for rt, names in by_rt.items():
        if rt != active:
            delete += names

    def _create_claim(name: str) -> None:
        custom_api.create_namespaced_custom_object(
            group=EXT_GROUP, version=SBX_VERSION, namespace=DEMO_NAMESPACE,
            plural="sandboxclaims", body=claim_body(name, active),
        )
    _run_all(_create_claim, create)
    _run_all(_delete("sandboxclaims", EXT_GROUP), delete)

    for rt in by_rt:
        replicas = demo_state["warmPoolReplicas"] if rt == active else 0
        _patch_once(
            custom_api, "sandboxwarmpools", f"agent-{rt}-warmpool",
            {"spec": {"replicas": replicas}}, force,
        )


def _snapshot_then_suspend(name: str) -> None:
    """Takes a manual snapshot of a session, waits for it, then suspends the Sandbox.
    Suspend alone takes no snapshot: a later resume would restore an older one."""
    _, _, custom_api = _get_k8s_clients()
    ops = demo_state["sessionOps"]
    if DEMO_MODE == "snapshots-rootfs":
        # What the session holds right before the snapshot, to compare after the resume.
        core_v1, _, _ = _get_k8s_clients()
        demo_state["preSuspend"].pop(name, None)
        before = _read_session_state(core_v1, name)
        if before:
            demo_state["preSuspend"][name] = before
        demo_state["survival"].pop(name, None)
    t0 = time.time()
    trigger = f"{name}-snap-{int(t0)}"
    try:
        custom_api.create_namespaced_custom_object(
            group=SNAP_GROUP, version=SNAP_VERSION, namespace=DEMO_NAMESPACE,
            plural="podsnapshotmanualtriggers",
            body={
                "apiVersion": f"{SNAP_GROUP}/{SNAP_VERSION}",
                "kind": "PodSnapshotManualTrigger",
                "metadata": {
                    "name": trigger,
                    "namespace": DEMO_NAMESPACE,
                    "labels": {PART_OF_LABEL: PART_OF, "session": name},
                },
                "spec": {"targetPod": name},
            },
        )
        while time.time() - t0 < SNAPSHOT_TIMEOUT_SECONDS:
            obj = custom_api.get_namespaced_custom_object(
                group=SNAP_GROUP, version=SNAP_VERSION, namespace=DEMO_NAMESPACE,
                plural="podsnapshotmanualtriggers", name=trigger,
            )
            if (obj.get("status", {}).get("snapshotCreated") or {}).get("name"):
                break
            time.sleep(3)
        else:
            ops[name] = {"state": "error", "error": "snapshot timed out; session left running"}
            return
        demo_state["snapshotLatencies"].append(round(time.time() - t0, 2))
        del demo_state["snapshotLatencies"][:-MAX_LATENCY_SAMPLES]
        ops[name] = {"state": "suspending"}
        custom_api.patch_namespaced_custom_object(
            group=AGENTS_GROUP, version=SBX_VERSION, namespace=DEMO_NAMESPACE,
            plural="sandboxes", name=name, body={"spec": {"operatingMode": "Suspended"}},
        )
        ops.pop(name, None)
    except Exception as e:
        logger.warning(f"Snapshot/suspend of {name} failed: {e}")
        ops[name] = {"state": "error", "error": str(e)[:200]}


def start_session_action(action: str) -> int:
    """Suspend: snapshot then suspend every running session, in the background.
    Resume: set every suspended (or failed) session back to Running. Returns the count."""
    core_v1, _, custom_api = _get_k8s_clients()
    sandboxes = _list_sessions(custom_api)
    pods = _list_pods(core_v1, get_runtime_config()["podLabel"])
    ops = demo_state["sessionOps"]
    states = classify_sessions(sandboxes, pods, ops)["states"]
    if action == "suspend":
        names = [n for n, s in states.items() if s == "running"]
        for n in names:
            ops[n] = {"state": "snapshotting"}
            _ops_executor.submit(_snapshot_then_suspend, n)
        return len(names)

    names = [n for n, s in states.items() if s in ("suspended", "error")]

    def _resume(name: str) -> None:
        ops.pop(name, None)
        custom_api.patch_namespaced_custom_object(
            group=AGENTS_GROUP, version=SBX_VERSION, namespace=DEMO_NAMESPACE,
            plural="sandboxes", name=name, body={"spec": {"operatingMode": "Running"}},
        )
        if states[name] == "suspended":
            demo_state["resumeStartedAt"][name] = time.time()
            if name in demo_state["preSuspend"]:
                demo_state["pendingSurvival"][name] = time.time()
    _run_all(_resume, names)
    return len(names)


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
            lat = data.get("latencies", {})
            sessions = summary.get("sessions") or {}

            entry = {
                "timestamp": data.get("timestamp"),
                "targetAgents": summary.get("targetAgents", 0),
                "activeAgents": summary.get("activeAgents", 0),
                "targetSandboxes": summary.get("targetSandboxes", 0),
                "readySandboxes": summary.get("readySandboxes", 0),
                "pendingSandboxes": summary.get("pendingSandboxes", 0),
                "warmReady": summary.get("warmPool", {}).get("ready", 0),
                "suspendedSessions": sessions.get("suspended", 0),
                "sandboxRuntime": summary.get("sandboxRuntime", "microvm"),
                "totalNodes": summary.get("totalNodes", 0),
                "readyNodes": summary.get("readyNodes", 0),
                "suspendedNodes": summary.get("suspendedNodes", 0),
                "inProgressNodes": summary.get("inProgressNodes", 0),
                "nodesByFamily": dict(summary.get("nodesByFamily", {})),
                "schedLatency": lat.get("scheduling", {}).get("avg", 0.0),
                "bootLatency": lat.get("sandboxBoot", {}).get("avg", 0.0),
            }

            demo_state["history"].append(entry)
            if len(demo_state["history"]) > MAX_HISTORY_LEN:
                demo_state["history"].pop(0)

        except Exception as e:
            logger.error(f"Error in telemetry sampler: {e}")

        if (
            not demo_state["mock_mode"]
            and time.time() - demo_state["lastReconcile"] >= RECONCILE_SECONDS
        ):
            try:
                await asyncio.to_thread(reconcile_agents)
            except Exception as e:
                logger.warning(f"Reconcile failed: {e}")

        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# API Models & Endpoints
# ---------------------------------------------------------------------------


class ScaleRequest(BaseModel):
    targetAgents: int = Field(..., ge=0, le=2000)
    sandboxRuntime: Optional[str] = Field(
        default=None, pattern="^(microvm|gvisor)$"
    )
    warmPoolReplicas: Optional[int] = Field(default=None, ge=0, le=200)
    activeBufferReplicas: Optional[int] = Field(default=None, ge=0, le=50)
    standbyReplicas: Optional[int] = Field(default=None, ge=0, le=100)
    consolidationDelayMinutes: Optional[int] = Field(
        default=None, ge=1, le=1440
    )


class QuotaUpdateRequest(BaseModel):
    quotaName: str
    cpuLimit: int = Field(..., ge=8, le=2000)


class SessionRequest(BaseModel):
    action: str = Field(..., pattern="^(suspend|resume)$")


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
    demo_state["resumeLatencies"].clear()
    demo_state["snapshotLatencies"].clear()
    demo_state["survival"].clear()
    demo_state["pendingSurvival"].clear()
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


def _apply_scale_live(runtime_switched: bool, buffers_changed: bool, consolidation: Optional[int]) -> Dict[str, int]:
    _, _, custom_api = _get_k8s_clients()
    reconcile_agents(force=runtime_switched)
    dist: Dict[str, int] = {}
    if buffers_changed or runtime_switched:
        dist = apply_buffers(custom_api)
    if consolidation is not None:
        classes = (
            [get_runtime_config()["computeClass"]]
            if SESSIONS
            else ["agent-sandbox-demo-microvm", "agent-sandbox-demo-gvisor"]
        )
        for cc_name in classes:
            try:
                custom_api.patch_cluster_custom_object(
                    group="cloud.google.com",
                    version="v1",
                    plural="computeclasses",
                    name=cc_name,
                    body={"spec": {"autoscalingPolicy": {"consolidationDelayMinutes": consolidation}}},
                )
            except Exception as e:
                logger.warning(f"Failed to patch ComputeClass {cc_name}: {e}")
    return dist


@app.post("/api/scale")
async def set_scale(req: ScaleRequest):
    """Sets the agent target, runtime, warm pool size, buffers and consolidation delay."""
    if not demo_state["mock_mode"]:
        await asyncio.to_thread(_mark_adopted)
    prev_runtime = demo_state.get("sandboxRuntime", "microvm")
    if req.sandboxRuntime is not None and not SESSIONS:
        demo_state["sandboxRuntime"] = req.sandboxRuntime
    rt_cfg = get_runtime_config(demo_state["sandboxRuntime"])
    runtime_switched = rt_cfg["runtime"] != prev_runtime

    demo_state["targetAgents"] = req.targetAgents
    demo_state["state_version"] = demo_state.get("state_version", 0) + 1
    demo_state["latest_telemetry"] = None
    if req.warmPoolReplicas is not None and not SESSIONS:
        demo_state["warmPoolReplicas"] = req.warmPoolReplicas
    if req.activeBufferReplicas is not None:
        demo_state["activeBufferReplicas"] = req.activeBufferReplicas
    if req.standbyReplicas is not None:
        demo_state["standbyReplicas"] = req.standbyReplicas
    if req.consolidationDelayMinutes is not None:
        demo_state["consolidationDelayMinutes"] = req.consolidationDelayMinutes

    standby_dist = distribute_standby_replicas(
        demo_state["standbyReplicas"], list(rt_cfg["standbyTemplates"])
    )
    if not demo_state["mock_mode"]:
        try:
            await asyncio.to_thread(
                _apply_scale_live,
                runtime_switched,
                req.activeBufferReplicas is not None or req.standbyReplicas is not None,
                req.consolidationDelayMinutes,
            )
            logger.info(
                f"Target {req.targetAgents} agents ({DEMO_MODE}, {rt_cfg['runtime']}), "
                f"warmPool={demo_state['warmPoolReplicas']}, activeBuffer={demo_state['activeBufferReplicas']}, "
                f"standby={demo_state['standbyReplicas']} ({standby_dist}), "
                f"consolidationDelayMinutes={demo_state['consolidationDelayMinutes']}"
            )
        except Exception as e:
            logger.warning(f"Live cluster scale skipped: {e}")

    return {
        "status": "success",
        "mode": DEMO_MODE,
        "sandboxRuntime": rt_cfg["runtime"],
        "sandboxRuntimeLabel": rt_cfg["label"],
        "targetAgents": req.targetAgents,
        "targetSandboxes": req.targetAgents,
        "warmPoolReplicas": demo_state["warmPoolReplicas"] if rt_cfg["warmPool"] else 0,
        "activeBufferReplicas": demo_state["activeBufferReplicas"],
        "standbyReplicas": demo_state["standbyReplicas"],
        "standbyByFamily": standby_dist,
        "consolidationDelayMinutes": demo_state["consolidationDelayMinutes"],
    }


@app.post("/api/sessions")
async def session_action(req: SessionRequest):
    """Snapshots modes: suspend every running session (snapshot first) or resume them."""
    if not SESSIONS:
        raise HTTPException(status_code=400, detail="Sessions need DEMO_MODE=snapshots or snapshots-rootfs")
    demo_state["state_version"] = demo_state.get("state_version", 0) + 1
    demo_state["latest_telemetry"] = None
    if demo_state["mock_mode"]:
        demo_state["mockSuspended"] = req.action == "suspend"
        if req.action == "resume":
            demo_state["mockResumed"] = True
        else:
            demo_state["mockResumed"] = False
        return {"status": "success", "action": req.action, "sessions": demo_state["targetAgents"]}
    try:
        count = await asyncio.to_thread(start_session_action, req.action)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{req.action} failed: {e}")
    logger.info(f"{req.action}: {count} sessions")
    return {"status": "success", "action": req.action, "sessions": count}


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
                group=BUFFER_GROUP,
                version=BUFFER_VERSION,
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
    return HTMLResponse(f"<h2>{TITLE}</h2>")


def main():
    parser = argparse.ArgumentParser(description=TITLE)
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
        help="Initial sandbox runtime in stateless mode (microvm or gvisor)",
    )
    args = parser.parse_args()

    if not SESSIONS:
        demo_state["sandboxRuntime"] = args.runtime

    if args.mock:
        demo_state["mock_mode"] = True
        logger.info("Running in forced mock simulation mode")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
