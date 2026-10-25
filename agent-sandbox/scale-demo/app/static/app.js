// GKE Agent Sandbox & ComputeClass Surge Observatory Frontend

let nodeChart, agentChart, latencyChart;
let lastTelemetry = null;
let clientHistory = [];
let currentResetEpoch = 0;
let chartWindowSeconds = 300; // Default 5m window (options: 30, 60, 300, 900, 1800, 3600)
let currentFamilyOrder = ["c3", "n4", "c4", "c2", "n2"];
let currentMode = null; // "stateless" | "snapshots" | "snapshots-rootfs", from the server
const SLIDER_MAX = { stateless: 2000, snapshots: 400, "snapshots-rootfs": 400 };
const isSessionMode = (m) => m === "snapshots" || m === "snapshots-rootfs";
const MODE_LABELS = { stateless: "stateless", snapshots: "Pod Snapshots, whole-pod", "snapshots-rootfs": "Pod Snapshots, rootfs-only" };

const FAMILY_PALETTE = {
  c3: { border: "#10b981", bg: "rgba(16, 185, 129, 0.55)", chipBg: "rgba(16, 185, 129, 0.2)", chipText: "#34d399" },
  n4: { border: "#3b82f6", bg: "rgba(59, 130, 246, 0.55)", chipBg: "rgba(59, 130, 246, 0.2)", chipText: "#60a5fa" },
  c4: { border: "#8b5cf6", bg: "rgba(139, 92, 246, 0.55)", chipBg: "rgba(139, 92, 246, 0.2)", chipText: "#a78bfa" },
  c2: { border: "#f59e0b", bg: "rgba(245, 158, 11, 0.55)", chipBg: "rgba(245, 158, 11, 0.2)", chipText: "#fbbf24" },
  n2: { border: "#94a3b8", bg: "rgba(148, 163, 184, 0.55)", chipBg: "rgba(148, 163, 184, 0.2)", chipText: "#cbd5e1" },
  // Snapshots mode groups nodes by machine type (one family, size fallback only)
  "n4-standard-8": { border: "#3b82f6", bg: "rgba(59, 130, 246, 0.55)", chipBg: "rgba(59, 130, 246, 0.2)", chipText: "#60a5fa" },
  "n4-standard-16": { border: "#8b5cf6", bg: "rgba(139, 92, 246, 0.55)", chipBg: "rgba(139, 92, 246, 0.2)", chipText: "#a78bfa" },
  "n4-standard-4": { border: "#f59e0b", bg: "rgba(245, 158, 11, 0.55)", chipBg: "rgba(245, 158, 11, 0.2)", chipText: "#fbbf24" }
};

const FALLBACK_PALETTE = [
  { border: "#06b6d4", bg: "rgba(6, 182, 212, 0.55)", chipBg: "rgba(6, 182, 212, 0.2)", chipText: "#22d3ee" },
  { border: "#ec4899", bg: "rgba(236, 72, 153, 0.55)", chipBg: "rgba(236, 72, 153, 0.2)", chipText: "#f472b6" },
  { border: "#14b8a6", bg: "rgba(20, 184, 166, 0.55)", chipBg: "rgba(20, 184, 166, 0.2)", chipText: "#2dd4bf" },
  { border: "#f97316", bg: "rgba(249, 115, 22, 0.55)", chipBg: "rgba(249, 115, 22, 0.2)", chipText: "#fb923c" },
  { border: "#6366f1", bg: "rgba(99, 102, 241, 0.55)", chipBg: "rgba(99, 102, 241, 0.2)", chipText: "#818cf8" }
];

function getFamilyStyle(fam, idx = 0) {
  if (FAMILY_PALETTE[fam]) return FAMILY_PALETTE[fam];
  return FALLBACK_PALETTE[idx % FALLBACK_PALETTE.length];
}

function buildNodeChartDatasets(familyOrder) {
  const ds = familyOrder.map((fam, idx) => {
    const style = getFamilyStyle(fam, idx);
    const roleTag = idx === 0
      ? "Primary P0"
      : (idx === familyOrder.length - 1 ? `Floor P${idx}` : `Fallback P${idx}`);
    return {
      label: `${fam} (${roleTag})`,
      familyKey: fam,
      data: [],
      borderColor: style.border,
      backgroundColor: style.bg,
      fill: idx === 0 ? "origin" : "-1",
      stack: "nodes"
    };
  });
  ds.push({
    label: "In-progress scale-up",
    familyKey: "__in_progress__",
    data: [],
    borderColor: "#38bdf8",
    borderDash: [4, 4],
    fill: false
  });
  return ds;
}

// Clock
setInterval(() => {
  const now = new Date();
  document.getElementById("clock").innerText = now.toUTCString().split(" ")[4] + " UTC";
}, 1000);

// Initialize Charts
function initCharts() {
  const commonOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 200 },
    interaction: {
      mode: "index",
      intersect: false
    },
    elements: {
      point: {
        radius: 0,
        hitRadius: 8,
        hoverRadius: 4
      },
      line: {
        borderWidth: 2,
        tension: 0.15
      }
    },
    scales: {
      x: {
        grid: { color: "rgba(255, 255, 255, 0.05)" },
        ticks: { color: "#94a3b8", maxTicksLimit: 8, maxRotation: 0 }
      },
      y: {
        grid: { color: "rgba(255, 255, 255, 0.05)" },
        ticks: { color: "#94a3b8" }
      }
    },
    plugins: {
      legend: { labels: { color: "#cbd5e1" } }
    }
  };

  // 1. Node Count by Family (Dynamic Stacked Area + In-Progress line)
  const ctxNodes = document.getElementById("nodeFamilyChart").getContext("2d");
  nodeChart = new Chart(ctxNodes, {
    type: "line",
    data: {
      labels: [],
      datasets: buildNodeChartDatasets(currentFamilyOrder)
    },
    options: {
      ...commonOptions,
      scales: {
        ...commonOptions.scales,
        y: { ...commonOptions.scales.y, stacked: true, min: 0 }
      }
    }
  });

  // 2. Agents & MicroVMs Pod Count
  const ctxAgents = document.getElementById("agentPodChart").getContext("2d");
  agentChart = new Chart(ctxAgents, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Target agents", data: [], borderColor: "#38bdf8", borderDash: [5, 5], fill: false },
        { label: "Active agents", data: [], borderColor: "#38bdf8", backgroundColor: "rgba(56, 189, 248, 0.14)", fill: true },
        { label: "Warm pool ready", data: [], borderColor: "#cbd5e1", borderDash: [2, 2], fill: false },
        { label: "Ready sandboxes", data: [], borderColor: "#34d399", backgroundColor: "rgba(52, 211, 153, 0.14)", fill: true },
        { label: "Pending sandboxes", data: [], borderColor: "#fbbf24", fill: false }
      ]
    },
    options: commonOptions
  });

  // 3. Scheduling & Boot Latencies
  const ctxLatency = document.getElementById("latencyChart").getContext("2d");
  latencyChart = new Chart(ctxLatency, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Pod scheduling latency (s)", data: [], borderColor: "#06b6d4", fill: false },
        { label: "Sandbox creation latency (s)", data: [], borderColor: "#ec4899", fill: false }
      ]
    },
    options: {
      ...commonOptions,
      scales: {
        ...commonOptions.scales,
        y: { ...commonOptions.scales.y, min: 0 }
      }
    }
  });
}

// Update UI Elements from Telemetry Payload
function updateUI(payload) {
  if (!payload || !payload.current) return;
  const current = payload.current;
  const summary = current.summary || {};
  const lat = current.latencies || {};
  const cc = current.computeClass || {};
  const buf = current.buffer || {};

  // Top header badges
  const clusterBadgeEl = document.getElementById("cluster-name-badge");
  if (clusterBadgeEl && summary.clusterName) {
    clusterBadgeEl.innerText = summary.clusterName;
  }

  const runtimeBadgeEl = document.getElementById("runtime-badge-val");
  if (runtimeBadgeEl) {
    runtimeBadgeEl.innerText = summary.sandboxRuntimeLabel || (summary.sandboxRuntime === "gvisor" ? "gVisor (runsc)" : "microVM (Kata)");
  }

  if (summary.mode && summary.mode !== currentMode) {
    applyModeLayout(summary.mode);
  }

  if (summary.sandboxRuntime) {
    const rtRadio = document.querySelector(`input[name="runtime"][value="${summary.sandboxRuntime}"]`);
    if (rtRadio && !rtRadio.checked) {
      rtRadio.checked = true;
    }
  }

  const healthBadge = document.getElementById("class-health-badge");
  if (cc.topLevel && cc.topLevel.healthy) {
    healthBadge.className = "badge health-badge healthy";
    healthBadge.innerHTML = "Class health: <strong>Healthy</strong>";
  } else {
    healthBadge.className = "badge health-badge unhealthy";
    const msg = cc.topLevel && cc.topLevel.messages ? cc.topLevel.messages[0] : "Degraded";
    healthBadge.innerHTML = `Class health: <strong>${msg}</strong>`;
  }

  document.getElementById("rule0-share-val").innerText = (summary.rule0Share ?? 100) + "%";
  document.getElementById("fallback-share-val").innerText = (summary.fallbackShare ?? 0) + "%";
  const actRep = buf.activeReplicas ?? 2;
  const stbyRep = buf.standbyReplicas ?? 6;
  const consMins = summary.consolidationDelayMinutes ?? 1;
  const bufSummaryEl = document.getElementById("buffer-summary-val");
  if (bufSummaryEl) {
    bufSummaryEl.innerText = `${actRep} active / ${stbyRep} standby | Consolidate: ${consMins}m`;
  }

  const wu = summary.capacityWarmup || { ready: true };
  const wuBadge = document.getElementById("warmup-badge");
  if (wuBadge) {
    wuBadge.classList.toggle("healthy", !!wu.ready);
    wuBadge.title = wu.detail || "";
    document.getElementById("warmup-val").innerText = wu.ready
      ? "ready to scale"
      : `warming up (${wu.detail || "building buffers"})`;
  }

  if (window.Guide) window.Guide.update(summary, lat, currentMode);

  // KPIs
  document.getElementById("kpi-total-nodes").innerText = summary.totalNodes || 0;
  const inProgEl = document.getElementById("kpi-inprogress-nodes");
  if (inProgEl) inProgEl.innerText = summary.inProgressNodes || 0;
  const suspEl = document.getElementById("kpi-suspended-nodes");
  if (suspEl) suspEl.innerText = summary.suspendedNodes || 0;

  const nfam = summary.nodesByFamily || {};
  const famOrder = (Array.isArray(summary.familyOrder) && summary.familyOrder.length > 0)
    ? summary.familyOrder
    : ["c3", "n4", "c4", "c2", "n2"];

  if (famOrder.join(",") !== currentFamilyOrder.join(",")) {
    currentFamilyOrder = [...famOrder];
    if (nodeChart) {
      nodeChart.data.datasets = buildNodeChartDatasets(currentFamilyOrder);
    }
  }

  const chipsContainer = document.getElementById("kpi-family-chips");
  if (chipsContainer) {
    chipsContainer.innerHTML = currentFamilyOrder.map((fam, idx) => {
      const st = getFamilyStyle(fam, idx);
      const count = nfam[fam] || 0;
      return `<span class="chip" style="background:${st.chipBg}; color:${st.chipText};">${fam}: <strong id="kpi-node-${fam}">${count}</strong></span>`;
    }).join("");
  }

  const activeAgentsNow = summary.activeAgents ?? (summary.readySandboxes || 0);
  document.getElementById("kpi-active-agents").innerText = activeAgentsNow;
  updateAgentDetail(summary, lat);
  document.getElementById("kpi-target-agents").innerText = summary.targetAgents || 0;
  document.getElementById("kpi-target-uvms").innerText = (summary.targetSandboxes !== undefined ? summary.targetSandboxes : summary.targetMicrovms) || 0;
  document.getElementById("kpi-ready-uvms").innerText = (summary.readySandboxes !== undefined ? summary.readySandboxes : summary.readyMicrovms) || 0;
  document.getElementById("kpi-pending-uvms").innerText = (summary.pendingSandboxes !== undefined ? summary.pendingSandboxes : summary.pendingMicrovms) || 0;

  const schedAvg = lat.scheduling ? lat.scheduling.avg : 0.0;
  const bootAvg = lat.sandboxBoot ? lat.sandboxBoot.avg : (lat.microvmBoot ? lat.microvmBoot.avg : 0.0);
  const schedP95 = lat.scheduling ? lat.scheduling.p95 : 0.0;

  document.getElementById("kpi-sched-lat").innerText = schedAvg + "s";
  document.getElementById("kpi-boot-lat").innerText = bootAvg + "s";
  document.getElementById("kpi-sched-p95").innerText = schedP95 + "s";
  const sinceEl = document.getElementById("kpi-metrics-since");
  if (sinceEl && lat.metricsSince) {
    sinceEl.innerText = `Window: since ${lat.metricsSince}`;
  }
  const sampleEl = document.getElementById("kpi-sample-count");
  if (sampleEl) {
    sampleEl.innerText = lat.sampleCount ?? 0;
  }

  const actBufEl = document.getElementById("kpi-active-buf");
  if (actBufEl) actBufEl.innerText = `${actRep} nodes`;
  const stbyLabelEl = document.getElementById("kpi-standby-label");
  if (stbyLabelEl) {
    stbyLabelEl.innerText = `Standby (${currentFamilyOrder.join("/")})`;
  }
  const stbyBufEl = document.getElementById("kpi-standby-buf");
  if (stbyBufEl && buf.standbyByFamily) {
    const sf = buf.standbyByFamily;
    stbyBufEl.innerText = currentFamilyOrder.map(fam => sf[fam] || 0).join("/");
  }

  document.getElementById("kpi-fulfillment-ratio").innerText = `${summary.rule0Share ?? 100}% : ${summary.fallbackShare ?? 0}%`;
  document.getElementById("rule0-progress-bar").style.width = (summary.rule0Share ?? 100) + "%";
  document.getElementById("fallback-progress-bar").style.width = (summary.fallbackShare ?? 0) + "%";

  const nAnn = summary.nodesByAnnotation || {};
  const rule0Fam = currentFamilyOrder[0] || "c3";
  const r0Nodes = Math.max(nAnn["0"] || 0, nfam[rule0Fam] || 0);
  document.getElementById("kpi-rule0-nodes").innerText = r0Nodes;
  const totalFallbackNodes = Math.max(0, (summary.totalNodes || 0) - r0Nodes);
  document.getElementById("kpi-fallback-nodes").innerText = totalFallbackNodes;

  // Render Matrix Table
  renderMatrixTable(cc.priorities || [], summary, current.quotas || {}, buf);

  // Sync History Buffer & Reset Epoch
  if (payload.resetEpoch !== undefined && payload.resetEpoch !== currentResetEpoch) {
    currentResetEpoch = payload.resetEpoch;
    clientHistory = [];
  }

  if (clientHistory.length === 0 && payload.history && payload.history.length > 0) {
    clientHistory = payload.history.slice(-3600);
  } else if (payload.latestPoint && payload.latestPoint.timestamp) {
    const lastPt = clientHistory[clientHistory.length - 1];
    if (!lastPt || lastPt.timestamp !== payload.latestPoint.timestamp) {
      clientHistory.push(payload.latestPoint);
      if (clientHistory.length > 3600) {
        clientHistory.shift();
      }
    }
  } else if (payload.history && payload.history.length > clientHistory.length) {
    clientHistory = payload.history.slice(-3600);
  }

  updateCharts();
}

// Fallback specs used only if ComputeClass priorities have not loaded yet
const DEFAULT_RULE_SPECS = [
  { identifier: "0", family: "c3", name: "Tier 0: c3 (c3-standard-8)", note: "Primary tier · CapacityQuota (24 vCPU)" },
  { identifier: "1", family: "n4", name: "Tier 1: n4 (n4-standard-8)", note: "Fallback tier · CapacityQuota (96 vCPU)" },
  { identifier: "2", family: "c4", name: "Tier 2: c4 (c4-standard-8)", note: "Fallback tier · CapacityQuota (96 vCPU)" },
  { identifier: "3", family: "c2", name: "Tier 3: c2 (c2-standard-8)", note: "Fallback tier · CapacityQuota (96 vCPU)" },
  { identifier: "4", family: "n2", name: "Tier 4: n2 (n2 family floor)", note: "Elastic catch-all floor · Class cap (720 vCPU)" }
];

const STATUS_DEFINITIONS = {
  Active: {
    label: "Active",
    gkeCondition: "Ready / Active",
    brief: "All zones & quotas available for scale-up",
    explanation: "All zones and machine configurations in this priority tier have available GCE capacity and are below their CapacityQuota limit. Cluster Autoscaler will provision nodes here first when pods are pending."
  },
  QuotaCapped: {
    label: "CAPPED · CAPACITYQUOTA",
    gkeCondition: "Pre-flight CapacityQuota / GCE Quota Cap",
    brief: "Quota ceiling reached — spilling over to next tier",
    explanation: "This tier has reached 100% of its configured CapacityQuota (or regional GCE CPU limit). Because Cluster Autoscaler enforces CapacityQuota pre-flight before calling GCE, it skips proposing new nodes in this tier and immediately spills over to the next priority rule without triggering a GCE ProvisioningSuspended error."
  },
  ProvisioningConstrained: {
    label: "CONSTRAINED · STOCKOUT",
    gkeCondition: "ProvisioningConstrained (OutOfResources / CapacityConstrained)",
    brief: "Partial zonal stockout — healthy zones still scaling",
    explanation: "Partial GCE capacity cooldown: a subset of zones in this tier hit a GCE stockout (OutOfResources) and are in temporary backoff, while remaining healthy zones in this same tier can still scale up."
  },
  ProvisioningSuspended: {
    label: "SUSPENDED · STOCKOUT / GCE QUOTA",
    gkeCondition: "ProvisioningSuspended (OutOfResources / QuotaExceeded)",
    brief: "Full tier in GCE backoff — falling back to next tier",
    explanation: "Full tier GCE backoff: every zone in this priority rule failed GCE VM provisioning due to hardware stockout (OutOfResources) or GCP project quota exhaustion (QuotaExceeded). Cluster Autoscaler skips this tier and falls back to the next priority rule until the backoff retry timer expires."
  },
  NodeProvisioningInProgress: {
    label: "PROVISIONING",
    gkeCondition: "NodeProvisioningInProgress",
    brief: "GCE VMs currently booting in this tier",
    explanation: "Cluster Autoscaler has triggered GCE instance creation in this priority tier and is waiting for the new VM nodes to boot and register with Kubernetes."
  }
};

let activePopoverTarget = null; // "legend" or rule identifier ("0".."N")
let activePopoverPinned = false;
let lastPopoverAnchorRect = null;
let latestPrioritiesById = {};

function renderPopoverContent(targetId) {
  const pop = document.getElementById("status-popover");
  if (!pop) return;

  if (targetId === "legend") {
    const pinHint = activePopoverPinned
      ? '<button class="status-popover-close" onclick="closeStatusPopover(event)" title="Unpin &amp; close">&times;</button>'
      : '<small class="text-muted">Click ? to pin</small>';
    pop.innerHTML = `
      <div class="status-popover-header">
        <span>Status &amp; spillover disambiguation guide</span>
        ${pinHint}
      </div>
      <div class="legend-item">
        <span class="status-badge badge-active">Active</span>
        <div style="margin-top:3px;"><strong>All zones &amp; quotas available:</strong> Ready for immediate scale-up.</div>
      </div>
      <div class="legend-item">
        <span class="status-badge badge-capped">CAPPED &middot; CAPACITYQUOTA</span>
        <div style="margin-top:3px;"><strong>Pre-flight quota ceiling (100%):</strong> Tier reached its Kubernetes <code>CapacityQuota</code> (or regional GCE quota). Autoscaler stops proposing nodes here pre-flight and spills over to the next priority tier without entering a GCE failure backoff.</div>
      </div>
      <div class="legend-item">
        <span class="status-badge badge-constrained">CONSTRAINED &middot; STOCKOUT</span>
        <div style="margin-top:3px;"><strong>Partial zonal stockout (<code>ProvisioningConstrained</code>):</strong> A subset of zones hit GCE <code>OutOfResources</code> cooldown, while healthy zones in this tier continue scaling.</div>
      </div>
      <div class="legend-item">
        <span class="status-badge badge-suspended">SUSPENDED &middot; STOCKOUT / GCE QUOTA</span>
        <div style="margin-top:3px;"><strong>Full tier GCE backoff (<code>ProvisioningSuspended</code>):</strong> Every zone in this tier failed GCE provisioning due to hardware stockout (<code>OutOfResources</code>) or GCP regional quota (<code>QuotaExceeded</code>). Skips to next tier until retry timer expires.</div>
      </div>
      <div class="legend-item">
        <span class="status-badge badge-provisioning">PROVISIONING</span>
        <div style="margin-top:3px;"><strong>Scale-up in progress (<code>NodeProvisioningInProgress</code>):</strong> GCE VMs are actively booting and joining the cluster.</div>
      </div>
    `;
    return;
  }

  const pStat = latestPrioritiesById[targetId] || {
    identifier: targetId,
    name: "Tier " + targetId,
    state: "Active",
    reason: "",
    reasons: [],
    stateMessage: "",
    backoff: { remainingSeconds: 0 },
    inProgressNodes: 0,
    isQuotaCapped: false,
    isGceQuotaCapped: false,
    quotaInfo: null
  };

  let defKey = pStat.state;
  if (pStat.state === "Active" && (pStat.isQuotaCapped || pStat.isGceQuotaCapped)) {
    defKey = "QuotaCapped";
  }
  const def = STATUS_DEFINITIONS[defKey] || STATUS_DEFINITIONS.Active;
  const rem = pStat.backoff ? pStat.backoff.remainingSeconds : 0;
  const timerHtml = rem > 0
    ? `<div style="margin-top:6px; color:#fca5a5;"><strong>GCE backoff retry timer:</strong> ${Math.floor(rem / 60)}m ${rem % 60}s remaining (${pStat.backoff.untilUtc || ""})</div>`
    : "";

  const reasonTags = (pStat.reasons && pStat.reasons.length > 0)
    ? pStat.reasons.join(", ")
    : (pStat.reason || "None");

  const qInfo = pStat.quotaInfo;
  const quotaSummaryHtml = qInfo
    ? `<div style="margin-top:6px;"><strong>Governing limit:</strong> <code>${qInfo.scopeLabel}</code> &mdash; ${qInfo.usedCpu} / ${qInfo.limitCpu} vCPU (${qInfo.utilizationPct}%${qInfo.capped ? " &middot; CAPPED" : ""})</div>`
    : "";

  const rawMsgHtml = pStat.stateMessage
    ? `<div class="status-popover-msg"><strong>GKE condition message (reason: ${reasonTags}):</strong><br>${pStat.stateMessage}</div>`
    : `<div style="margin-top:6px; font-size:0.75rem;" class="text-muted"><strong>GKE priorityStatuses condition:</strong> None (no GCE provisioning errors)</div>`;

  const pinHint = activePopoverPinned
    ? '<button class="status-popover-close" onclick="closeStatusPopover(event)" title="Unpin &amp; close">&times;</button>'
    : '<small class="text-muted">Click badge to pin</small>';

  pop.innerHTML = `
    <div class="status-popover-header">
      <span>${pStat.name || ("Rule #" + targetId)} &mdash; ${pStat.badgeLabel || def.label}</span>
      ${pinHint}
    </div>
    <div><strong>GKE Condition:</strong> <code>${def.gkeCondition}</code></div>
    <div style="margin-top:6px;">${def.explanation}</div>
    ${quotaSummaryHtml}
    ${timerHtml}
    ${rawMsgHtml}
  `;
}

function positionPopover(rect, targetId) {
  const pop = document.getElementById("status-popover");
  if (!pop || !rect) return;
  const popWidth = pop.offsetWidth || 370;
  const popHeight = pop.offsetHeight || 190;

  let left;
  let top;

  if (targetId === "legend") {
    left = rect.right + 14;
    if (left + popWidth > window.innerWidth - 16) {
      left = Math.max(16, rect.left - popWidth - 14);
    }
    top = Math.max(16, Math.min(window.innerHeight - popHeight - 16, rect.top - 4));
  } else {
    left = rect.right + 16;
    if (left + popWidth > window.innerWidth - 16) {
      left = Math.max(16, rect.left - popWidth - 16);
    }
    top = Math.max(16, Math.min(window.innerHeight - popHeight - 16, rect.top - 8));
  }

  pop.style.top = `${top}px`;
  pop.style.left = `${left}px`;
}

function showStatusPopover(targetId, anchorEl, pin = false) {
  const pop = document.getElementById("status-popover");
  if (!pop) return;
  if (!pin && activePopoverPinned) return;

  if (pin) {
    if (activePopoverPinned && activePopoverTarget === targetId) {
      closeStatusPopover();
      return;
    }
    activePopoverPinned = true;
    pop.classList.add("pinned");
  } else {
    pop.classList.remove("pinned");
  }

  activePopoverTarget = targetId;
  if (anchorEl) {
    lastPopoverAnchorRect = anchorEl.getBoundingClientRect();
  }
  renderPopoverContent(targetId);
  pop.classList.remove("hidden");
  positionPopover(lastPopoverAnchorRect, targetId);
}

function hideHoverStatusPopover() {
  if (activePopoverPinned) return;
  const pop = document.getElementById("status-popover");
  if (pop) {
    pop.classList.add("hidden");
    pop.classList.remove("pinned");
  }
  activePopoverTarget = null;
}

function closeStatusPopover(e) {
  if (e) e.stopPropagation();
  activePopoverPinned = false;
  activePopoverTarget = null;
  const pop = document.getElementById("status-popover");
  if (pop) {
    pop.classList.add("hidden");
    pop.classList.remove("pinned");
  }
  document.querySelectorAll(".status-badge.pinned").forEach(el => el.classList.remove("pinned"));
}

function renderMatrixTable(priorities, summary, quotas, buf) {
  const tbody = document.getElementById("matrix-tbody");

  const nAnn = summary.nodesByAnnotation || {};
  const nFam = summary.nodesByFamily || {};
  const stbyFam = (buf && buf.standbyByFamily) ? buf.standbyByFamily : {};

  const rowsToRender = (Array.isArray(priorities) && priorities.length > 0)
    ? priorities
    : DEFAULT_RULE_SPECS;

  const validRowIds = new Set(rowsToRender.map(r => String(r.identifier)));
  Array.from(tbody.querySelectorAll("tr[data-row-id]")).forEach(existingTr => {
    if (!validRowIds.has(existingTr.getAttribute("data-row-id"))) {
      existingTr.remove();
    }
  });

  rowsToRender.forEach((pStat, idx) => {
    const ruleId = String(pStat.identifier ?? idx);
    const fam = pStat.family || currentFamilyOrder[idx] || `tier${idx}`;
    const ruleName = pStat.name || `Tier ${ruleId}: ${fam}`;
    const ruleNote = pStat.note || "";
    const coresPerNode = pStat.coresPerNode || 8;

    latestPrioritiesById[ruleId] = pStat;

    const isPinned = activePopoverPinned && activePopoverTarget === ruleId;
    const pinnedClass = isPinned ? " pinned" : "";
    const badgeLabel = pStat.badgeLabel || pStat.state || "Active";
    const rem = pStat.backoff ? (pStat.backoff.remainingSeconds || 0) : 0;
    const mm = String(Math.floor(rem / 60)).padStart(2, "0");
    const ss = String(rem % 60).padStart(2, "0");
    const timerSpan = rem > 0 ? ` <span class="countdown">(${mm}:${ss})</span>` : "";

    let badgeClass = "badge-active";
    if (pStat.state === "ProvisioningSuspended") {
      badgeClass = "badge-suspended";
    } else if (badgeLabel.startsWith("CAPPED")) {
      badgeClass = "badge-capped";
    } else if (pStat.state === "ProvisioningConstrained") {
      badgeClass = "badge-constrained";
    } else if (pStat.state === "NodeProvisioningInProgress") {
      badgeClass = "badge-provisioning";
    }

    let statusBadge = `<span class="status-badge ${badgeClass}${pinnedClass}" data-rule-id="${ruleId}">${badgeLabel} <span class="help-pill">i</span></span>${timerSpan}`;
    if (pStat.secondaryBadge) {
      const secClass = pStat.secondaryBadge.startsWith("CAPPED") ? "badge-capped" : "badge-constrained";
      statusBadge += ` <span class="status-badge ${secClass}" data-rule-id="${ruleId}">${pStat.secondaryBadge}</span>`;
    }
    if (pStat.inProgressNodes > 0 && pStat.state !== "NodeProvisioningInProgress") {
      statusBadge += ` <span class="status-badge badge-provisioning" data-rule-id="${ruleId}">+${pStat.inProgressNodes} scaling</span>`;
    } else if (pStat.inProgressNodes > 0) {
      statusBadge += ` <small class="text-info">(+${pStat.inProgressNodes} nodes)</small>`;
    }

    const briefText = pStat.causeSummary || (STATUS_DEFINITIONS[pStat.state] || STATUS_DEFINITIONS.Active).brief;

    // Nodes (Active family count / Annotated count / Suspended S3 vs Standby buffer target)
    const suspFam = summary.suspendedByFamily || {};
    const famCount = nFam[fam] || 0;
    const annCount = nAnn[ruleId] || 0;
    const suspCount = suspFam[fam] || 0;
    const stbyTarget = stbyFam[fam] || 0;
    const nodeText = `${famCount} active (idx=${annCount}, standby=${suspCount}/${stbyTarget} S3)`;

    // CPU Info
    const cpuCur = (pStat.cpu && pStat.cpu.current > 0) ? pStat.cpu.current : (famCount * coresPerNode);
    const cpuTgt = (pStat.cpu && pStat.cpu.target > 0) ? pStat.cpu.target : ((famCount + (pStat.inProgressNodes || 0)) * coresPerNode);
    const cpuPct = pStat.cpu ? pStat.cpu.utilizationPct : 0.0;

    // Unified Quota Progress Bar across all rows (Row #0 GCE Quota, Rows #1..N-1 Family CapacityQuota, Floor Class Cap)
    let quotaHtml = '<span class="text-muted">Unbounded</span>';
    const q = pStat.quotaInfo;
    if (q && q.limitCpu > 0) {
      const capped = Boolean(q.capped);
      quotaHtml = `
        <div class="quota-bar-wrapper">
          <div class="quota-scope-label" title="${q.scopeLabel}">${q.scopeLabel}</div>
          <div style="display:flex; justify-content:space-between; font-size:0.75rem;">
            <span>${q.usedCpu} / ${q.limitCpu}c</span>
            <span class="${capped ? 'text-warning font-bold' : ''}">${q.utilizationPct}%</span>
          </div>
          <div class="quota-bar">
            <div class="quota-fill ${capped ? 'capped' : ''}" style="width: ${Math.min(100, q.utilizationPct)}%;"></div>
          </div>
        </div>
      `;
    }

    // Scaling events
    const ev = pStat.scalingEvents || {};
    const evText = `+${ev.provisioned || 0} / -${ev.consolidated || 0} / ~${ev.migrated || 0}`;

    let tr = tbody.querySelector(`tr[data-row-id="${ruleId}"]`);
    if (!tr) {
      tr = document.createElement("tr");
      tr.setAttribute("data-row-id", ruleId);
      tr.innerHTML = `
        <td class="col-id"><strong>#${ruleId}</strong></td>
        <td class="col-name"></td>
        <td class="col-status">
          <div class="status-cell-wrapper" data-rule-id="${ruleId}">
            <div class="status-badge-slot"></div>
            <span class="status-brief"></span>
          </div>
        </td>
        <td class="col-nodes"></td>
        <td class="col-cpu"></td>
        <td class="col-quota"></td>
        <td class="col-events"></td>
      `;
      const cellWrapper = tr.querySelector(".status-cell-wrapper");
      cellWrapper.addEventListener("mouseenter", () => showStatusPopover(ruleId, cellWrapper, false));
      cellWrapper.addEventListener("mouseleave", () => hideHoverStatusPopover());
      cellWrapper.addEventListener("click", e => {
        e.stopPropagation();
        showStatusPopover(ruleId, cellWrapper, true);
      });
      tbody.appendChild(tr);
    }

    tr.querySelector(".col-name").innerHTML = `<strong>${ruleName}</strong><br><small class="text-muted">${ruleNote}</small>`;
    tr.querySelector(".status-badge-slot").innerHTML = statusBadge;
    tr.querySelector(".status-brief").textContent = briefText;
    tr.querySelector(".col-nodes").innerHTML = `<strong>${nodeText}</strong>`;
    tr.querySelector(".col-cpu").textContent = `${cpuCur} / ${cpuTgt}c (${cpuPct}%)`;
    tr.querySelector(".col-quota").innerHTML = quotaHtml;
    tr.querySelector(".col-events").innerHTML = `<code>${evText}</code>`;
  });

  if (activePopoverTarget) {
    renderPopoverContent(activePopoverTarget);
  }
}

function formatWindowLabel(sec) {
  if (sec < 60) return `${sec}s`;
  return `${Math.round(sec / 60)}m`;
}

function setChartWindow(seconds) {
  chartWindowSeconds = seconds;
  document.querySelectorAll("#time-window-group .btn-time-window").forEach(btn => {
    const w = parseInt(btn.getAttribute("data-window"), 10);
    btn.classList.toggle("active", w === seconds);
  });
  updateCharts();
}

function downsampleHistory(rawSlice, maxPoints = 60) {
  if (!rawSlice || rawSlice.length <= maxPoints) {
    return rawSlice || [];
  }
  const step = Math.ceil(rawSlice.length / maxPoints);
  const thinned = [];
  for (let i = 0; i < rawSlice.length - 1; i += step) {
    thinned.push(rawSlice[i]);
  }
  // Always include the latest data point
  thinned.push(rawSlice[rawSlice.length - 1]);
  return thinned;
}

// Chart Updater
function updateCharts() {
  const rawWindow = clientHistory.slice(-chartWindowSeconds);
  const history = downsampleHistory(rawWindow, 60);
  const metaEl = document.getElementById("chart-history-meta");
  if (metaEl) {
    const thinNote = rawWindow.length > history.length ? ` (${history.length} pts plotted)` : "";
    metaEl.innerText = `Showing last ${formatWindowLabel(chartWindowSeconds)} (${rawWindow.length}s window / ${clientHistory.length}s buffered)${thinNote}`;
  }

  // Show small point dots only on 30s zoom; hide dots on wider windows to prevent congestion
  const ptRadius = chartWindowSeconds <= 30 ? 2 : 0;
  [nodeChart, agentChart, latencyChart].forEach(chart => {
    if (chart && chart.options && chart.options.elements && chart.options.elements.point) {
      chart.options.elements.point.radius = ptRadius;
    }
  });

  const labels = history.map(h => {
    if (!h.timestamp) return "";
    const parts = h.timestamp.split("T");
    return parts[1] ? parts[1].split(".")[0] : "";
  });

  // Node Chart (dynamically mapped from currentFamilyOrder)
  nodeChart.data.labels = labels;
  currentFamilyOrder.forEach((fam, idx) => {
    if (nodeChart.data.datasets[idx]) {
      nodeChart.data.datasets[idx].data = history.map(h => {
        if (h.nodesByFamily && h.nodesByFamily[fam] !== undefined) {
          return h.nodesByFamily[fam];
        }
        return h[`nodes_${fam}`] || 0;
      });
    }
  });
  const inProgIdx = currentFamilyOrder.length;
  if (nodeChart.data.datasets[inProgIdx]) {
    nodeChart.data.datasets[inProgIdx].data = history.map(h => h.inProgressNodes || 0);
  }
  nodeChart.update("none");

  // Agent Chart
  agentChart.data.labels = labels;
  agentChart.data.datasets[0].data = history.map(h => h.targetAgents || 0);
  agentChart.data.datasets[1].data = history.map(h => h.activeAgents || 0);
  agentChart.data.datasets[2].data = history.map(h => (isSessionMode(currentMode) ? h.suspendedSessions : h.warmReady) || 0);
  agentChart.data.datasets[3].data = history.map(h => (h.readySandboxes !== undefined ? h.readySandboxes : (h.readyMicrovms || 0)));
  agentChart.data.datasets[4].data = history.map(h => (h.pendingSandboxes !== undefined ? h.pendingSandboxes : (h.pendingMicrovms || 0)));
  agentChart.update("none");

  // Latency Chart
  latencyChart.data.labels = labels;
  latencyChart.data.datasets[0].data = history.map(h => h.schedLatency || 0.0);
  latencyChart.data.datasets[1].data = history.map(h => h.bootLatency || 0.0);
  latencyChart.update("none");
}

// Slider & Control Listeners
const slider = document.getElementById("agent-slider");
const sliderVal = document.getElementById("target-agents-val");
slider.addEventListener("input", e => {
  sliderVal.innerText = e.target.value;
});

function setPreset(val) {
  slider.value = val;
  sliderVal.innerText = val;
  applySurge();
}

async function applySurge() {
  const applyBtn = document.getElementById("apply-btn");
  const targetAgents = parseInt(slider.value, 10) || 0;
  const runtimeRadio = document.querySelector('input[name="runtime"]:checked');
  const sandboxRuntime = runtimeRadio ? runtimeRadio.value : "microvm";
  const warmEl = document.getElementById("warm-pool-input");
  let warmPoolReplicas = warmEl ? parseInt(warmEl.value, 10) : 10;
  if (Number.isNaN(warmPoolReplicas)) warmPoolReplicas = 10;
  warmPoolReplicas = Math.max(0, Math.min(200, warmPoolReplicas));
  if (warmEl) warmEl.value = warmPoolReplicas;
  const activeBufEl = document.getElementById("active-buffer-input");
  const standbyBufEl = document.getElementById("buffer-input");

  let activeReplicas = activeBufEl ? parseInt(activeBufEl.value, 10) : 2;
  if (Number.isNaN(activeReplicas)) activeReplicas = 2;
  activeReplicas = Math.max(0, Math.min(50, activeReplicas));
  if (activeBufEl) activeBufEl.value = activeReplicas;

  let standbyReplicas = standbyBufEl ? parseInt(standbyBufEl.value, 10) : 6;
  if (Number.isNaN(standbyReplicas)) standbyReplicas = 6;
  standbyReplicas = Math.max(0, Math.min(100, standbyReplicas));
  if (standbyBufEl) standbyBufEl.value = standbyReplicas;

  const consEl = document.getElementById("consolidation-delay-input");
  let consDelay = consEl ? parseInt(consEl.value, 10) : 1;
  if (Number.isNaN(consDelay)) consDelay = 1;
  consDelay = Math.max(1, Math.min(1440, consDelay));
  if (consEl) consEl.value = consDelay;

  if (applyBtn) {
    applyBtn.disabled = true;
    applyBtn.innerText = "Applying...";
  }

  try {
    const res = await fetch("/api/scale", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        targetAgents: targetAgents,
        sandboxRuntime: sandboxRuntime,
        warmPoolReplicas: warmPoolReplicas,
        activeBufferReplicas: activeReplicas,
        standbyReplicas: standbyReplicas,
        consolidationDelayMinutes: consDelay
      })
    });
    const result = await res.json();
    console.log("Scale applied:", result);

    const stateRes = await fetch("/api/state");
    if (stateRes.ok) {
      const stateData = await stateRes.json();
      lastTelemetry = stateData;
      updateUI(stateData);
    }

    if (applyBtn) {
      applyBtn.innerText = "Applied!";
      setTimeout(() => {
        applyBtn.disabled = false;
        applyBtn.innerText = "Apply surge";
      }, 900);
    }
  } catch (err) {
    console.error("Scale error:", err);
    if (applyBtn) {
      applyBtn.disabled = false;
      applyBtn.innerText = "Apply surge";
    }
  }
}

document.getElementById("apply-btn").addEventListener("click", applySurge);

// Stateless: runtime toggle + warm pool. Snapshots: one runtime (gVisor), no warm pool
// (a restore needs a fresh pod), and suspend/resume controls.
function applyModeLayout(mode) {
  currentMode = mode;
  const snap = isSessionMode(mode);
  const rootfs = mode === "snapshots-rootfs";
  const toggle = (id, show) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", !show);
  };
  toggle("runtime-group", !snap);
  toggle("warm-pool-group", !snap);
  toggle("session-group", snap);
  toggle("kpi-warm-row", !snap);
  toggle("kpi-session-row", snap);
  toggle("kpi-snaplat-row", snap);
  toggle("kpi-survival-row", rootfs);
  document.querySelectorAll(".preset-row[data-mode]").forEach(el => {
    el.classList.toggle("hidden", el.dataset.mode !== mode);
  });
  document.getElementById("mode-badge-val").innerText = MODE_LABELS[mode] || mode;
  document.getElementById("kpi-agents-title").innerText = snap
    ? "Agents (one session Sandbox each)"
    : "Agents (one SandboxClaim each)";
  document.getElementById("panel-sub").innerText = rootfs
    ? "One gVisor session Sandbox per agent on the wide multi-family gVisor ComputeClass. A rootfs-only snapshot restores across machine families, but keeps only files on the root filesystem (not /tmp) and restarts the process, so memory is lost"
    : snap
    ? "One gVisor session Sandbox per agent on a single machine family (n4), so whole-pod snapshots restore on any node; the ComputeClass falls back by size only"
    : "One SandboxClaim per agent, served from a SandboxWarmPool on microVM (Kata) or gVisor (runsc), backed by active and multi-family standby CapacityBuffers and ComputeClass fallback tiers";
  document.getElementById("agent-chart-title").innerText = snap
    ? "Active agents, suspended sessions & sandbox pods over time"
    : "Active agents, warm pool & sandbox pods over time";
  const max = SLIDER_MAX[mode] || 2000;
  slider.max = max;
  document.getElementById("slider-max-val").innerText = max.toLocaleString();
  if (agentChart) {
    agentChart.data.datasets[2].label = snap ? "Suspended sessions" : "Warm pool ready";
    agentChart.update("none");
  }
}

function updateAgentDetail(summary, lat) {
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.innerText = v;
  };
  if (isSessionMode(currentMode)) {
    const ss = summary.sessions || {};
    set("kpi-sess-suspended", ss.suspended || 0);
    set("kpi-sess-snap", ss.snapshotting || 0);
    set("kpi-sess-error", ss.error || 0);
    set("kpi-snap-lat", ((lat.snapshot && lat.snapshot.avg) || 0) + "s");
    set("kpi-snap-count", (lat.snapshot && lat.snapshot.count) || 0);
    set("kpi-resume-lat", ((lat.resume && lat.resume.avg) || 0) + "s");
    set("kpi-resume-count", (lat.resume && lat.resume.count) || 0);
    const sv = summary.resumeSurvival;
    if (sv) {
      const total = sv.checked || 0;
      set("kpi-surv-files", `${sv.fileKept || 0} / ${total}`);
      set("kpi-surv-mem", `${sv.memoryKept || 0} / ${total}`);
      set("kpi-surv-note", (sv.pending ? `${sv.pending} still reading` : "read from session logs") + (sv.simulated ? " (simulated)" : ""));
    } else {
      set("kpi-surv-files", "-");
      set("kpi-surv-mem", "-");
      set("kpi-surv-note", "resume a snapshotted session to read it");
    }
    return;
  }
  const wp = summary.warmPool || {};
  set("kpi-warm-ready", wp.ready || 0);
  set("kpi-warm-target", wp.target || 0);
  const cr = lat.claimReady || {};
  set("kpi-claim-lat", (cr.avg || 0) + "s");
  set("kpi-warm-hits", `${cr.warmHits || 0} / ${cr.count || 0}`);
}

async function sessionAction(action) {
  const btn = document.getElementById(action === "suspend" ? "suspend-btn" : "resume-btn");
  const label = btn.innerText;
  btn.disabled = true;
  btn.innerText = action === "suspend" ? "Snapshotting..." : "Resuming...";
  try {
    const res = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action })
    });
    const result = await res.json();
    console.log(`Sessions ${action}:`, result);
    btn.innerText = res.ok ? `${result.sessions} sessions` : "Failed";
  } catch (err) {
    console.error("Session action error:", err);
    btn.innerText = "Failed";
  }
  setTimeout(() => {
    btn.disabled = false;
    btn.innerText = label;
  }, 1500);
}

async function resetMetrics() {
  const resetBtn = document.getElementById("reset-btn");
  if (resetBtn) {
    resetBtn.disabled = true;
    resetBtn.innerText = "Resetting...";
  }
  try {
    const res = await fetch("/api/reset", { method: "POST" });
    if (res.ok) {
      clientHistory = [];
      const stateRes = await fetch("/api/state");
      if (stateRes.ok) {
        const stateData = await stateRes.json();
        lastTelemetry = stateData;
        updateUI(stateData);
      }
    }
    if (resetBtn) {
      resetBtn.innerText = "Reset!";
      setTimeout(() => {
        resetBtn.disabled = false;
        resetBtn.innerText = "Reset data & metrics";
      }, 900);
    }
  } catch (err) {
    console.error("Reset error:", err);
    if (resetBtn) {
      resetBtn.disabled = false;
      resetBtn.innerText = "Reset data & metrics";
    }
  }
}

const resetBtnEl = document.getElementById("reset-btn");
if (resetBtnEl) {
  resetBtnEl.addEventListener("click", resetMetrics);
}

// SSE Telemetry Stream
function connectSSE() {
  const statusEl = document.getElementById("sse-status");
  const evtSource = new EventSource("/api/stream");

  evtSource.addEventListener("telemetry", e => {
    try {
      const data = JSON.parse(e.data);
      lastTelemetry = data;
      updateUI(data);
      statusEl.className = "status-indicator connected";
      statusEl.innerText = "Live SSE connected";
    } catch (err) {
      console.error("SSE parse error:", err);
    }
  });

  evtSource.onerror = () => {
    statusEl.className = "status-indicator disconnected";
    statusEl.innerText = "Disconnected (Reconnecting...)";
  };
}

// Initial fetch & connect
window.addEventListener("DOMContentLoaded", async () => {
  initCharts();

  const colHelpEl = document.getElementById("status-col-help");
  if (colHelpEl) {
    colHelpEl.addEventListener("mouseenter", () => showStatusPopover("legend", colHelpEl, false));
    colHelpEl.addEventListener("mouseleave", () => hideHoverStatusPopover());
    colHelpEl.addEventListener("click", e => {
      e.stopPropagation();
      showStatusPopover("legend", colHelpEl, true);
    });
  }

  document.addEventListener("click", e => {
    const pop = document.getElementById("status-popover");
    if (activePopoverPinned && pop && !pop.contains(e.target)) {
      closeStatusPopover();
    }
  });

  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    if (data && data.current) {
      lastTelemetry = data;
      if (data.current.summary) {
        slider.value = data.current.summary.targetAgents || 0;
        sliderVal.innerText = slider.value;
        const consEl = document.getElementById("consolidation-delay-input");
        if (consEl && data.current.summary.consolidationDelayMinutes !== undefined) {
          consEl.value = data.current.summary.consolidationDelayMinutes;
        }
      }
      if (data.current.buffer) {
        const activeBufEl = document.getElementById("active-buffer-input");
        const standbyBufEl = document.getElementById("buffer-input");
        if (activeBufEl && data.current.buffer.activeReplicas !== undefined) {
          activeBufEl.value = data.current.buffer.activeReplicas;
        }
        if (standbyBufEl && data.current.buffer.standbyReplicas !== undefined) {
          standbyBufEl.value = data.current.buffer.standbyReplicas;
        }
      }
      updateUI(data);
    }
  } catch (e) {
    console.error("Initial state fetch error:", e);
  }
  connectSSE();
});
