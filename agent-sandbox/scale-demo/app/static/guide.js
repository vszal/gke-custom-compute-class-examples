// Optional guided tour for the control tower. Steps are data (STEPS); status is
// auto-detected from the latest telemetry where there is a signal, else the user marks it.
// Exposes window.Guide.update(summary, lat, mode) (called from app.js) and window.Guide.toggle().

const STEPS = {
  stateless: [
    {
      id: "capacity",
      title: "Wait for Capacity: ready to scale",
      text: "The amber Capacity badge in the header, at the top of the page, turns green when every CapacityBuffer is ready and the standby nodes are suspended. You can scale earlier, but then you measure cold node starts, not the warm layer.",
      show: "warmup-badge",
      done: (c) => c.summary.capacityWarmup ? !!c.summary.capacityWarmup.ready : true,
      latch: true,
    },
    {
      id: "small",
      title: "Press a small preset (10)",
      text: "Each agent is one SandboxClaim, served from the warm pool when a pre-booted sandbox is ready. Watch Claim → Ready and Warm hits in the Agents card; Rule-0 share should stay at 100%.",
      done: (c) => c.n(c.summary.targetAgents) > 0 && c.n(c.summary.readySandboxes) >= c.n(c.summary.targetAgents),
      latch: true,
    },
    {
      id: "runtime",
      title: "Optional: switch the sandbox runtime",
      text: (c) => `You are on ${c.runtimeLabel}. Switching to the other runtime (microVM or gVisor) scales the other runtime's claims and warm pool to 0 and repoints the buffers. Re-run the same preset to compare latency. The Warm pool input sets pool replicas live.`,
      done: (c) => !!c.firstRuntime && c.summary.sandboxRuntime !== c.firstRuntime,
      latch: true,
      optional: true,
    },
    {
      id: "large",
      title: "Press a large preset (150, 300 or 500)",
      text: "Quotas cap each machine family, so a full tier spills to the next before GCE is asked. Watch the node-family chart, Fallback share rising, and the matrix's quota column. Tier 0 is c3, then n4, c4, c2, n2.",
      done: (c) => c.n(c.summary.targetAgents) >= 150 && c.n(c.summary.readySandboxes) >= c.n(c.summary.targetAgents),
      latch: true,
    },
    {
      id: "reset",
      title: "Scale to 0 and Reset data & metrics",
      text: "Press preset 0, wait for the agents to drain, then Reset data & metrics to clear chart history, latency averages and event counters before another run. A few empty Ready nodes can remain: they are the CapacityBuffers' warm capacity, which goes back to suspended or is removed after the consolidation delay.",
      done: (c) => c.isDone("large") && c.n(c.summary.targetAgents) === 0,
    },
    {
      id: "cleanup",
      title: "Clean up when finished",
      text: "Idle buffers and standby nodes still cost money. Run ./uninstall.sh to remove the claims, warm pools, buffers, quotas, classes and namespace.",
      manual: true,
    },
  ],
  snapshots: [
    {
      id: "capacity",
      title: "Wait for Capacity: ready to scale",
      text: "The amber Capacity badge in the header, at the top of the page, turns green when every CapacityBuffer is ready and the standby nodes are suspended. Scaling earlier measures cold node starts instead of the warm layer.",
      show: "warmup-badge",
      done: (c) => c.summary.capacityWarmup ? !!c.summary.capacityWarmup.ready : true,
      latch: true,
    },
    {
      id: "sessions",
      title: "Press preset 20 to start sessions",
      text: "Each agent is one session Sandbox on a single machine family (n4), so a whole-pod snapshot can restore on any node. Wait until Running matches the target.",
      done: (c) => c.n(c.summary.targetAgents) > 0 && c.n(c.summary.readySandboxes) >= c.n(c.summary.targetAgents),
      latch: true,
    },
    {
      id: "suspend",
      title: "Press the session button: Snapshot & suspend all",
      text: "With all sessions running, the session control next to Sessions (Pod Snapshots) reads Snapshot & suspend all. Each running session is snapshotted first, then set to Suspended and its pod deleted. Watch Snapshotting fall to 0, Suspended rise, and Snapshot latency fill in.",
      done: (c) => c.n(c.sessions.suspended) > 0,
      latch: true,
    },
    {
      id: "drain",
      title: "Watch the nodes drain",
      text: "With no pods left, the node-family chart should drop as nodes scale down (after the consolidation delay). This is the saving a snapshot buys. Mark it done when you have seen it.",
      manual: true,
    },
    {
      id: "resume",
      title: "Press the session button again: Resume all",
      text: "Once every session is suspended the same button reads Resume all (the Sessions pill shows the counts). Each session restores from its latest snapshot into a new pod. Read Resume → Ready in the Agents card once the count matches your sessions.",
      done: (c) => c.n(c.lat.resume && c.lat.resume.count) > 0,
      latch: true,
    },
    {
      id: "large",
      title: "Try a larger preset (100, then 250)",
      text: "100 sessions fit n4-standard-8 (rule 0). 250 shows size fallback to n4-standard-16. Live, size fallback appears only on a real stockout (see README Sharp edges).",
      done: (c) => c.n(c.summary.targetAgents) >= 100 && c.n(c.summary.readySandboxes) >= c.n(c.summary.targetAgents),
      latch: true,
      optional: true,
    },
    {
      id: "reset",
      title: "Scale to 0 and Reset data & metrics",
      text: "Press preset 0 and wait for the sessions to go, then Reset data & metrics to clear history and latency averages. A few empty Ready nodes can remain: they are the CapacityBuffers' warm capacity, which goes back to suspended or is removed after the consolidation delay.",
      done: (c) => c.isDone("resume") && c.n(c.summary.targetAgents) === 0,
    },
    {
      id: "cleanup",
      title: "Clean up when finished",
      text: "Run ./uninstall.sh to remove sessions, snapshot triggers and policies, buffers, quotas, classes and the namespace. It leaves the snapshot bucket; delete it with gcloud storage rm -r.",
      manual: true,
    },
  ],
  "snapshots-rootfs": [
    {
      id: "capacity",
      title: "Wait for Capacity: ready to scale",
      text: "The amber Capacity badge in the header, at the top of the page, turns green when every CapacityBuffer is ready and the standby nodes are suspended. Scaling earlier measures cold node starts instead of the warm layer.",
      show: "warmup-badge",
      done: (c) => c.summary.capacityWarmup ? !!c.summary.capacityWarmup.ready : true,
      latch: true,
    },
    {
      id: "sessions",
      title: "Press preset 20 to start sessions",
      text: "Each agent is one session Sandbox on the wide multi-family gVisor class (c3, n4, c4, c2, n2). A rootfs-only snapshot is not tied to a machine family, so the ladder can stay wide. Each session writes a counter to a file on its root filesystem and keeps another in memory.",
      done: (c) => c.n(c.summary.targetAgents) > 0 && c.n(c.summary.readySandboxes) >= c.n(c.summary.targetAgents),
      latch: true,
    },
    {
      id: "expect",
      title: "Know what the resume will and will not restore",
      text: "Restored: files on the root filesystem. Lost: everything in memory, running processes (the container starts again), and /tmp, which gVisor keeps in memory. After a resume the Agents card reports Files kept and Memory kept, read from each session's own log. Mark this done when you have read it.",
      manual: true,
    },
    {
      id: "suspend",
      title: "Press the session button: Snapshot & suspend all",
      text: "With all sessions running, the session control next to Sessions (Pod Snapshots) reads Snapshot & suspend all. The tower reads each session's state, snapshots it, then sets it to Suspended and deletes its pod. Watch Snapshotting fall to 0, Suspended rise, and Snapshot latency fill in.",
      done: (c) => c.n(c.sessions.suspended) > 0,
      latch: true,
    },
    {
      id: "drain",
      title: "Watch the nodes drain",
      text: "With no pods left, the node-family chart should drop as nodes scale down (after the consolidation delay). Mark it done when you have seen it.",
      manual: true,
    },
    {
      id: "resume",
      title: "Press the session button again: Resume all",
      text: "Once every session is suspended the same button reads Resume all (the Sessions pill shows the counts). Each session restores its root filesystem into a new pod, on whichever family the class provides, and its process starts again. Read Resume → Ready in the Agents card once the count matches your sessions.",
      done: (c) => c.n(c.lat.resume && c.lat.resume.count) > 0,
      latch: true,
    },
    {
      id: "survived",
      title: "Read what survived",
      text: "Files kept should read N of N and Memory kept 0 of N: the file counter carried on, the in-memory counter restarted. A Files kept of 0 of N means the restore did not apply (see README Sharp edges).",
      done: (c) => !!c.summary.resumeSurvival && c.n(c.summary.resumeSurvival.checked) > 0,
      latch: true,
    },
    {
      id: "large",
      title: "Try a larger preset (100, then 300)",
      text: "Quotas cap each machine family, so a full tier spills to the next. Sessions that land on a different family still restore, which a whole-pod snapshot would not do.",
      done: (c) => c.n(c.summary.targetAgents) >= 100 && c.n(c.summary.readySandboxes) >= c.n(c.summary.targetAgents),
      latch: true,
      optional: true,
    },
    {
      id: "reset",
      title: "Scale to 0 and Reset data & metrics",
      text: "Press preset 0 and wait for the sessions to go, then Reset data & metrics to clear history and latency averages. A few empty Ready nodes can remain: they are the CapacityBuffers' warm capacity, which goes back to suspended or is removed after the consolidation delay.",
      done: (c) => c.isDone("resume") && c.n(c.summary.targetAgents) === 0,
    },
    {
      id: "cleanup",
      title: "Clean up when finished",
      text: "Run ./uninstall.sh to remove sessions, snapshot triggers and policies, buffers, quotas, classes and the namespace. It leaves the snapshot bucket; delete it with gcloud storage rm -r.",
      manual: true,
    },
  ],
};

(function () {
  const KEY = "sbx-guide-state"; // "open" | "collapsed" | "closed"
  const latched = { stateless: new Set(), snapshots: new Set(), "snapshots-rootfs": new Set() };
  const manual = { stateless: new Set(), snapshots: new Set(), "snapshots-rootfs": new Set() };
  let firstRuntime = null;
  let last = { summary: {}, lat: {}, mode: null };
  let lastSig = "";
  let state = "open";

  function load() {
    try { return localStorage.getItem(KEY) || "open"; } catch (e) { return "open"; }
  }
  function save() {
    try { localStorage.setItem(KEY, state); } catch (e) { /* storage unavailable */ }
  }

  function context(mode) {
    const summary = last.summary || {};
    return {
      summary,
      lat: last.lat || {},
      sessions: summary.sessions || {},
      firstRuntime,
      runtimeLabel: summary.sandboxRuntimeLabel || "the current runtime",
      n: (v) => Number(v) || 0,
      isDone: (id) => latched[mode].has(id) || manual[mode].has(id),
    };
  }

  // Returns [{step, status}] with the first not-done required step marked "current".
  function evaluate(mode) {
    const steps = STEPS[mode] || [];
    const ctx = context(mode);
    const out = steps.map((step) => {
      let done = manual[mode].has(step.id) || latched[mode].has(step.id);
      if (!done && typeof step.done === "function") {
        let hit = false;
        try { hit = !!step.done(ctx); } catch (e) { hit = false; }
        if (hit) {
          done = true;
          if (step.latch) latched[mode].add(step.id);
        }
      }
      return { step, status: done ? "done" : "upcoming", ctx };
    });
    const cur = out.find((o) => o.status !== "done" && !o.step.optional) || out.find((o) => o.status !== "done");
    if (cur) cur.status = "current";
    return out;
  }

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };

  function render(items, mode) {
    const panel = document.getElementById("guide-panel");
    const btn = document.getElementById("guide-btn");
    if (!panel) return;
    panel.classList.toggle("hidden", state === "closed");
    panel.classList.toggle("collapsed", state === "collapsed");
    if (btn) btn.setAttribute("aria-pressed", state === "closed" ? "false" : "true");
    panel.replaceChildren();

    const doneCount = items.filter((i) => i.status === "done").length;
    const head = el("div", "guide-head");
    head.appendChild(el("span", "guide-title",
      `Guided tour: ${mode} (${doneCount}/${items.length})`));
    const collapse = el("button", "guide-icon-btn", state === "collapsed" ? "▲" : "▼");
    collapse.title = state === "collapsed" ? "Expand" : "Collapse";
    collapse.setAttribute("aria-label", collapse.title);
    collapse.onclick = () => setState(state === "collapsed" ? "open" : "collapsed");
    const close = el("button", "guide-icon-btn", "✕");
    close.title = "Close (reopen with the Guide button)";
    close.setAttribute("aria-label", "Close guide");
    close.onclick = () => setState("closed");
    head.append(collapse, close);
    panel.appendChild(head);

    const list = el("ol", "guide-steps");
    items.forEach(({ step, status, ctx }, idx) => {
      const li = el("li", `guide-step ${status}`);
      const mark = el("span", "guide-mark", status === "done" ? "✓" : String(idx + 1));
      const body = el("div", "guide-body");
      body.appendChild(el("div", "guide-step-title", step.title + (step.optional ? " (optional)" : "")));
      if (status === "current" || status === "upcoming") {
        body.appendChild(el("div", "guide-step-text", typeof step.text === "function" ? step.text(ctx) : step.text));
      }
      if (step.show && status !== "done") {
        const sm = el("button", "guide-mini-btn guide-show-btn", "Show me");
        sm.title = "Scroll to and highlight it";
        sm.onclick = () => {
          const t = document.getElementById(step.show);
          if (!t) return;
          t.scrollIntoView({ behavior: "smooth", block: "center" });
          t.classList.remove("flash-highlight");
          void t.offsetWidth;
          t.classList.add("flash-highlight");
          setTimeout(() => t.classList.remove("flash-highlight"), 2000);
        };
        body.appendChild(sm);
        body.appendChild(document.createTextNode(" "));
      }
      if (status !== "done" && (step.manual || status === "current")) {
        const b = el("button", "guide-mini-btn", step.manual ? "Mark done" : "Skip");
        b.onclick = () => { manual[mode].add(step.id); refresh(); };
        body.appendChild(b);
      }
      li.append(mark, body);
      list.appendChild(li);
    });
    panel.appendChild(list);

    const foot = el("div", "guide-foot");
    const restart = el("button", "guide-mini-btn", "Restart tour");
    restart.onclick = () => { latched[mode].clear(); manual[mode].clear(); refresh(); };
    foot.appendChild(restart);
    panel.appendChild(foot);
  }

  function refresh(force) {
    const mode = last.mode || "stateless";
    const items = evaluate(mode);
    const sig = [mode, state, last.summary.sandboxRuntime, ...items.map((i) => i.status)].join("|");
    if (!force && sig === lastSig) return;
    lastSig = sig;
    render(items, mode);
  }

  function setState(next) {
    state = next;
    save();
    refresh(true);
  }

  window.Guide = {
    update(summary, lat, mode) {
      last = { summary: summary || {}, lat: lat || {}, mode: mode || last.mode };
      if (!firstRuntime && summary && summary.sandboxRuntime) firstRuntime = summary.sandboxRuntime;
      refresh();
    },
    toggle() { setState(state === "closed" ? "open" : "closed"); },
  };

  state = load();
  document.addEventListener("DOMContentLoaded", () => refresh(true));
  if (document.readyState !== "loading") refresh(true);
})();
