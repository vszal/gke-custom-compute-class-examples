// Optional guided tour for the control tower. Data-driven: edit STEPS to change the tour.
// Each step: id, title, body (HTML), and an optional detect(ctx) -> bool that marks it done
// from telemetry. Steps without detect (or not yet detected) offer a manual "Mark done".

const GUIDE_STORAGE_KEY = "controlTowerGuide.v1";

const STEPS = {
  pause: [
    {
      id: "warm",
      title: "Wait for Capacity: ready to scale",
      body: "The amber Capacity badge in the header, at the top of the page, turns green when every CapacityBuffer is ready and the standby nodes are suspended (a few minutes after install). Scaling earlier works, but you would be measuring cold starts.",
      show: "capacity-badge",
      detect: (c) => c.summary.capacityWarmup === true
    },
    {
      id: "runtime",
      title: "Pick a sandbox runtime",
      body: "microVM (Kata) needs nested virtualization, so it stays on Intel families. gVisor runs on any family. Switching reconciles the other runtime's workers and buffers to 0."
    },
    {
      id: "small",
      title: "Scale small first",
      body: "Click the <b>100</b> preset. It claims warm active-buffer nodes and resumes suspended standby nodes.",
      detect: (c) => (c.summary.targetAgents || 0) > 0
    },
    {
      id: "placed",
      title: "Watch the pods land",
      body: "Ready sandboxes should reach the target on the charts and the Agents card. Read scheduling and boot latency on the KPI card.",
      detect: (c) => c.targetSandboxes > 0 && c.readySandboxes >= c.targetSandboxes
    },
    {
      id: "big",
      title: "Go big: fallback and quota caps",
      body: "Try <b>600</b>, then <b>1,200</b> or <b>2,000</b>. When a tier hits its CapacityQuota (<code>CAPPED</code>) the autoscaler spills to the next family; Rule-0 share drops and Fallback share rises.",
      detect: (c) => (c.summary.fallbackShare || 0) > 0 || c.familiesWithNodes >= 2 || c.capped
    },
    {
      id: "matrix",
      title: "Read the observability matrix",
      body: "Scroll to the matrix: per-tier status (Active, CAPPED, STOCKOUT), standby counts and quota bars. Click the <b>?</b> for definitions."
    },
    {
      id: "reset",
      title: "Reset data &amp; metrics",
      body: "Between experiments, click <b>Reset data &amp; metrics</b> to clear chart history and latency averages so the next run starts clean."
    },
    {
      id: "zero",
      title: "Scale back to 0",
      body: "Click the <b>0: Idle</b> preset. Workers go away; a few empty Ready nodes can remain as the CapacityBuffers' warm capacity until the consolidation delay passes.",
      detect: (c, done) => done("small") && (c.summary.targetAgents || 0) === 0
    },
    {
      id: "cleanup",
      title: "Clean up when finished",
      body: "Run <code>./uninstall.sh</code> to remove the demo workloads, buffers, quotas, both ComputeClasses and the namespace."
    }
  ],
  substrate: [
    {
      id: "warm",
      title: "Wait for Capacity: ready to scale",
      body: "The amber Capacity badge in the header, at the top of the page, turns green once every CapacityBuffer (including the per-family seed buffers) is ready and the standby nodes are suspended.",
      show: "capacity-badge",
      detect: (c) => c.summary.capacityWarmup === true
    },
    {
      id: "runtime",
      title: "Know your runtime's ladder",
      body: "microVM needs nested virtualization, so its ladder is Intel only (c4, n4, c3). gVisor adds the AMD families (c4d, n4d, c3d). Each runtime and family has its own WorkerPool."
    },
    {
      id: "goldens",
      title: "Check the golden snapshots",
      body: "Each family's ActorTemplate builds its own golden snapshot on its own family once workers run there. Wait for the templates to be ready before creating actors (see the README's Substrate mode section)."
    },
    {
      id: "small",
      title: "Scale small first",
      body: "Click the <b>100</b> preset. New demand goes to the first family in the ladder, one actor per worker.",
      detect: (c) => c.placedTotal > 0
    },
    {
      id: "actors",
      title: "Create, suspend and resume actors",
      body: "Run <code>./scripts/substrate-actor-load.sh --runtime=microvm --actors=3</code> to measure actor latency per family. Snapshots restore only on the family that took them, so actors return to their family's pool."
    },
    {
      id: "spill",
      title: "Go big and watch the spill",
      body: "Try <b>600</b> or more. When a family's workers stay unscheduled past the spill timer (quota full or stockout), the tower blocks that family and moves the rest to the next family's pool. Placed workers are never moved back.",
      detect: (c) => c.blockedFamilies > 0 || c.familiesWithPlaced >= 2
    },
    {
      id: "reset",
      title: "Reset data &amp; metrics",
      body: "Click <b>Reset data &amp; metrics</b> between runs to clear chart history and latency averages."
    },
    {
      id: "zero",
      title: "Scale back to 0",
      body: "Click the <b>0: Idle</b> preset. Unscheduled workers are removed first, then the lowest families.",
      detect: (c, done) => done("small") && (c.summary.targetAgents || 0) === 0
    },
    {
      id: "cleanup",
      title: "Clean up with uninstall.sh",
      body: "Run <code>./uninstall.sh</code>. It suspends and deletes the demo's actors, tags and templates, then removes the atespace, buffers, pools, quotas and classes. It leaves the <code>ate.dev</code> CRDs and the rest of Agent Substrate in place."
    }
  ]
};

const GuideTour = (() => {
  let state = { open: true, collapsed: false, manual: { pause: {}, substrate: {} } };
  let mode = "pause";
  let ctx = null;
  let panel = null;
  let lastSig = "";

  function load() {
    try {
      const raw = window.localStorage.getItem(GUIDE_STORAGE_KEY);
      if (raw) {
        const s = JSON.parse(raw);
        state = {
          open: s.open !== false,
          collapsed: !!s.collapsed,
          manual: { pause: (s.manual && s.manual.pause) || {}, substrate: (s.manual && s.manual.substrate) || {} }
        };
      }
    } catch (e) { /* storage unavailable: use defaults */ }
  }

  function save() {
    try { window.localStorage.setItem(GUIDE_STORAGE_KEY, JSON.stringify(state)); } catch (e) { /* ignore */ }
  }

  function buildCtx(current) {
    const summary = current.summary || {};
    const sub = current.substrate || {};
    const placed = sub.placed || {};
    const nfam = summary.nodesByFamily || {};
    const priorities = (current.computeClass && current.computeClass.priorities) || [];
    return {
      summary,
      targetSandboxes: (summary.targetSandboxes !== undefined ? summary.targetSandboxes : summary.targetMicrovms) || 0,
      readySandboxes: (summary.readySandboxes !== undefined ? summary.readySandboxes : summary.readyMicrovms) || 0,
      familiesWithNodes: Object.values(nfam).filter((n) => n > 0).length,
      capped: priorities.some((p) => /CAPPED/i.test(p.badgeLabel || "")),
      placedTotal: Object.values(placed).reduce((a, b) => a + b, 0),
      familiesWithPlaced: Object.values(placed).filter((n) => n > 0).length,
      blockedFamilies: (sub.blocked || []).length
    };
  }

  function statuses() {
    const steps = STEPS[mode] || [];
    const done = {};
    const isDone = (id) => !!done[id];
    steps.forEach((s) => {
      let d = !!state.manual[mode][s.id];
      if (!d && s.detect && ctx) {
        try { d = !!s.detect(ctx, isDone); } catch (e) { d = false; }
        if (d) state.manual[mode][s.id] = true; // latch: a later dip (standby nodes resuming) must not un-tick it
      }
      done[s.id] = d;
    });
    let currentSeen = false;
    return steps.map((s) => {
      let status = "upcoming";
      if (done[s.id]) status = "done";
      else if (!currentSeen) { status = "current"; currentSeen = true; }
      return { step: s, status, auto: !!s.detect };
    });
  }

  function render() {
    if (!panel) return;
    panel.classList.toggle("hidden", !state.open);
    const openBtn = document.getElementById("guide-open-btn");
    if (openBtn) openBtn.classList.toggle("active", state.open);
    if (!state.open) return;
    const rows = statuses();
    const nDone = rows.filter((r) => r.status === "done").length;
    const title = mode === "substrate" ? "Guided tour: Substrate mode" : "Guided tour: pause mode";
    const list = rows.map((r, i) => `
      <li class="guide-step guide-${r.status}">
        <span class="guide-dot" aria-hidden="true">${r.status === "done" ? "&#10003;" : i + 1}</span>
        <div class="guide-step-body">
          <div class="guide-step-title">${r.step.title}</div>
          <div class="guide-step-text">${r.step.body}</div>
          ${r.step.show && r.status !== "done" ? `<button type="button" class="guide-mark guide-show" data-show="${r.step.show}">Show me</button> ` : ""}
          ${r.status !== "done" ? `<button type="button" class="guide-mark" data-step="${r.step.id}">Mark done</button>` : ""}
        </div>
      </li>`).join("");
    panel.className = "guide-panel" + (state.collapsed ? " collapsed" : "");
    panel.innerHTML = `
      <div class="guide-header">
        <strong class="guide-title">${title}</strong>
        <span class="guide-progress">${nDone}/${rows.length}</span>
        <button type="button" class="guide-icon-btn" data-act="collapse" title="${state.collapsed ? "Expand" : "Collapse"}" aria-label="${state.collapsed ? "Expand guide" : "Collapse guide"}">${state.collapsed ? "&#9650;" : "&#9660;"}</button>
        <button type="button" class="guide-icon-btn" data-act="close" title="Close (reopen with the Guide button)" aria-label="Close guide">&times;</button>
      </div>
      ${state.collapsed ? "" : `<ol class="guide-steps">${list}</ol>
      <div class="guide-footer"><button type="button" class="guide-mark" data-act="restart">Restart tour</button></div>`}`;
  }

  function onClick(ev) {
    const t = ev.target.closest("[data-act],[data-step],[data-show]");
    if (!t) return;
    if (t.dataset.show) {
      const target = document.getElementById(t.dataset.show);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.remove("flash-highlight");
        void target.offsetWidth;
        target.classList.add("flash-highlight");
        setTimeout(() => target.classList.remove("flash-highlight"), 2000);
      }
      return;
    }
    if (t.dataset.step) state.manual[mode][t.dataset.step] = true;
    else if (t.dataset.act === "close") state.open = false;
    else if (t.dataset.act === "collapse") state.collapsed = !state.collapsed;
    else if (t.dataset.act === "restart") state.manual[mode] = {};
    save();
    render();
  }

  function init() {
    panel = document.getElementById("guide-panel");
    if (!panel) return;
    load();
    panel.addEventListener("click", onClick);
    const openBtn = document.getElementById("guide-open-btn");
    if (openBtn) {
      openBtn.addEventListener("click", () => {
        state.open = !state.open;
        if (state.open) state.collapsed = false;
        save();
        render();
      });
    }
    render();
  }

  function update(current) {
    if (!panel || !current) return;
    mode = (current.summary && current.summary.mode === "substrate") || (current.substrate && current.substrate.mode === "substrate") ? "substrate" : "pause";
    ctx = buildCtx(current);
    const sig = mode + JSON.stringify(statuses().map((r) => r.status)) + state.open + state.collapsed;
    if (sig !== lastSig) { lastSig = sig; render(); }
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
  }
  return { update, STEPS };
})();

if (typeof window !== "undefined") window.GuideTour = GuideTour;
