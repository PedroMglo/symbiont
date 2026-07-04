/* RAG Dashboard — SPA controller */
"use strict";

const STATE = {
  days: 7,
  autoRefresh: true,
  refreshInterval: null,
  charts: {},
  sse: null,
  eventCount: 0,
  eventErrors: 0,
  eventStart: Date.now(),
};

const API = "/dashboard";

// ─── Helpers ────────────────────────────────────────────────────────────────

function fmt(n, decimals = 1) {
  if (n == null || n === "" || isNaN(n)) return "—";
  return Number(n).toFixed(decimals);
}

function fmtInt(n) {
  if (n == null || n === "" || isNaN(n)) return "—";
  return Number(n).toLocaleString();
}

function fmtPct(n) {
  if (n == null || n === "" || isNaN(n)) return "—";
  return fmt(n) + "%";
}

function fmtMs(n) {
  if (n == null || n === "" || isNaN(n)) return "—";
  const v = Number(n);
  return v >= 1000 ? fmt(v / 1000) + "s" : Math.round(v) + "ms";
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtShortTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return (
    d.toLocaleDateString([], { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  );
}

function $(sel) {
  return document.querySelector(sel);
}
function $$(sel) {
  return document.querySelectorAll(sel);
}

function setKpi(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function setKpiClass(id, cls) {
  const el = document.getElementById(id);
  if (el) {
    el.classList.remove("success", "warning", "error");
    if (cls) el.classList.add(cls);
  }
}

async function fetchJson(path, params = {}) {
  const url = new URL(API + path, location.origin);
  Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

// ─── Chart Helpers ──────────────────────────────────────────────────────────

const CHART_COLORS = {
  accent: "#58a6ff",
  purple: "#a371f7",
  success: "#3fb950",
  warning: "#d29922",
  error: "#f85149",
  muted: "#556677",
};

function chartDefaults() {
  const style = getComputedStyle(document.documentElement);
  const textColor =
    style.getPropertyValue("--text-secondary").trim() || "#8899aa";
  const gridColor = style.getPropertyValue("--border").trim() || "#1e2a3a";
  return {
    responsive: true,
    maintainAspectRatio: true,
    aspectRatio: 2,
    plugins: {
      legend: {
        labels: { color: textColor, boxWidth: 10, font: { size: 11 } },
      },
    },
    scales: {
      x: {
        ticks: { color: textColor, font: { size: 10 } },
        grid: { color: gridColor },
      },
      y: {
        ticks: { color: textColor, font: { size: 10 } },
        grid: { color: gridColor },
      },
    },
  };
}

function makeOrUpdate(key, canvas, config) {
  if (STATE.charts[key]) {
    const chart = STATE.charts[key];
    chart.data = config.data;
    if (config.options) Object.assign(chart.options, config.options);
    chart.update("none");
  } else {
    const ctx = document.getElementById(canvas);
    if (!ctx) return;
    STATE.charts[key] = new Chart(ctx, config);
  }
}

function destroyCharts() {
  Object.values(STATE.charts).forEach((c) => c.destroy());
  STATE.charts = {};
}

// ─── Gauge Update ───────────────────────────────────────────────────────────

function setGauge(id, pct) {
  const circle = document.getElementById(id);
  const text = document.getElementById(id + "-text");
  if (!circle || !text) return;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const circumference = 2 * Math.PI * 50;
  const offset = circumference * (1 - v / 100);
  circle.style.strokeDasharray = circumference;
  circle.style.strokeDashoffset = offset;
  text.textContent = Math.round(v) + "%";
  if (v > 85) circle.style.stroke = "var(--error)";
  else if (v > 65) circle.style.stroke = "var(--warning)";
  else circle.style.stroke = "var(--accent)";
}

// ─── Tab: Overview ──────────────────────────────────────────────────────────

async function loadOverview() {
  const [summary, timeline] = await Promise.all([
    fetchJson("/summary", { days: STATE.days }),
    fetchJson("/timeline", {
      days: STATE.days,
      resolution: STATE.days <= 1 ? "minute" : "hour",
    }),
  ]);

  if (summary) {
    const r = summary.requests || {};
    const ret = summary.retrieval || {};
    const ing = summary.ingest || {};
    setKpi("kpi-requests", fmtInt(r.total_requests));
    setKpi("kpi-error-rate", fmtPct(r.error_rate));
    setKpiClass(
      "kpi-error-rate",
      r.error_rate > 5 ? "error" : r.error_rate > 1 ? "warning" : "success",
    );
    setKpi("kpi-avg-latency", fmtMs(r.avg_latency));
    setKpi("kpi-p95-latency", fmtMs(r.p95_latency));
    setKpi("kpi-retrievals", fmtInt(ret.total_retrievals));
    setKpi("kpi-ingest-status", ing.last_status || "—");
    setKpiClass(
      "kpi-ingest-status",
      ing.last_status === "ok"
        ? "success"
        : ing.last_status === "error"
          ? "error"
          : "",
    );

    const res = summary.resources || {};
    updateHealthBadge(r.error_rate);
  }

  if (timeline && timeline.data) {
    const labels = timeline.data.map((d) => fmtShortTime(d.time));
    makeOrUpdate("requests-timeline", "chart-requests-timeline", {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Requests",
            data: timeline.data.map((d) => d.requests),
            backgroundColor: CHART_COLORS.accent + "88",
            borderColor: CHART_COLORS.accent,
            borderWidth: 1,
          },
          {
            label: "Errors",
            data: timeline.data.map((d) => d.errors),
            backgroundColor: CHART_COLORS.error + "88",
            borderColor: CHART_COLORS.error,
            borderWidth: 1,
          },
        ],
      },
      options: {
        ...chartDefaults(),
        scales: {
          ...chartDefaults().scales,
          x: { ...chartDefaults().scales.x, stacked: true },
          y: { ...chartDefaults().scales.y, stacked: true },
        },
      },
    });

    makeOrUpdate("latency-timeline", "chart-latency-timeline", {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Avg",
            data: timeline.data.map((d) => d.avg_latency),
            borderColor: CHART_COLORS.accent,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: "P95",
            data: timeline.data.map((d) => d.p95_latency),
            borderColor: CHART_COLORS.warning,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
        ],
      },
      options: chartDefaults(),
    });
  }
}

// ─── Tab: Retrieval ─────────────────────────────────────────────────────────

async function loadRetrieval() {
  const data = await fetchJson("/retrieval", { days: STATE.days });
  if (!data) return;

  const s = data.summary || {};
  setKpi("kpi-acceptance", fmtPct(s.acceptance_rate));
  setKpiClass(
    "kpi-acceptance",
    s.acceptance_rate > 70
      ? "success"
      : s.acceptance_rate > 40
        ? "warning"
        : "error",
  );
  setKpi("kpi-best-score", fmt(s.avg_best_score, 3));
  setKpi("kpi-reranker", fmtPct(s.reranker_pct));
  setKpi("kpi-hyde", fmtPct(s.hyde_pct));
  setKpi("kpi-dedup", fmt(s.avg_dedup_removed));
  setKpi("kpi-ret-latency", fmtMs(s.avg_latency));

  if (data.timeline && data.timeline.length) {
    const labels = data.timeline.map((d) => fmtShortTime(d.time));
    makeOrUpdate("scores-timeline", "chart-scores-timeline", {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Avg Score",
            data: data.timeline.map((d) => d.avg_score),
            borderColor: CHART_COLORS.accent,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: "P25",
            data: data.timeline.map((d) => d.p25_score),
            borderColor: CHART_COLORS.muted,
            borderWidth: 1,
            borderDash: [4, 4],
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: "P75",
            data: data.timeline.map((d) => d.p75_score),
            borderColor: CHART_COLORS.purple,
            borderWidth: 1,
            borderDash: [4, 4],
            tension: 0.3,
            pointRadius: 0,
          },
        ],
      },
      options: chartDefaults(),
    });

    makeOrUpdate("acceptance-timeline", "chart-acceptance-timeline", {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Acceptance %",
            data: data.timeline.map((d) => d.acceptance_rate),
            borderColor: CHART_COLORS.success,
            backgroundColor: CHART_COLORS.success + "22",
            fill: true,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
        ],
      },
      options: chartDefaults(),
    });
  }

  if (data.route_modes && data.route_modes.length) {
    makeOrUpdate("route-modes", "chart-route-modes", {
      type: "doughnut",
      data: {
        labels: data.route_modes.map((d) => d.route_mode),
        datasets: [
          {
            data: data.route_modes.map((d) => d.value),
            backgroundColor: [
              CHART_COLORS.accent,
              CHART_COLORS.purple,
              CHART_COLORS.success,
              CHART_COLORS.warning,
              CHART_COLORS.error,
            ],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: getComputedStyle(document.documentElement)
                .getPropertyValue("--text-secondary")
                .trim(),
              font: { size: 11 },
            },
          },
        },
      },
    });
  }

  if (data.score_histogram && data.score_histogram.length) {
    makeOrUpdate("score-histogram", "chart-score-histogram", {
      type: "bar",
      data: {
        labels: data.score_histogram.map((d) => d.bucket),
        datasets: [
          {
            label: "Count",
            data: data.score_histogram.map((d) => d.value),
            backgroundColor: CHART_COLORS.accent + "aa",
          },
        ],
      },
      options: chartDefaults(),
    });
  }
}

// ─── Tab: Ingest ────────────────────────────────────────────────────────────

async function loadIngest() {
  const data = await fetchJson("/ingest", { days: STATE.days });
  if (!data) return;

  const latest = data.runs && data.runs[0];
  if (latest) {
    setKpi("kpi-ingest-last", latest.status);
    setKpiClass(
      "kpi-ingest-last",
      latest.status === "ok" ? "success" : "error",
    );
    setKpi("kpi-files-parsed", fmtInt(latest.files_parsed));
    setKpi("kpi-chunks-stored", fmtInt(latest.chunks_stored));
    setKpi("kpi-stale-deleted", fmtInt(latest.stale_deleted));
    setKpi("kpi-ingest-errors", fmtInt(latest.error_count));
    setKpiClass(
      "kpi-ingest-errors",
      latest.error_count > 0 ? "error" : "success",
    );
    setKpi(
      "kpi-ingest-duration",
      latest.duration_s != null ? fmt(latest.duration_s) + "s" : "—",
    );
  }

  if (data.stages && data.stages.length) {
    makeOrUpdate("stage-latency", "chart-stage-latency", {
      type: "bar",
      data: {
        labels: data.stages.map((d) => d.stage_name),
        datasets: [
          {
            label: "Avg ms",
            data: data.stages.map((d) => d.avg_ms),
            backgroundColor: CHART_COLORS.accent + "aa",
          },
          {
            label: "Max ms",
            data: data.stages.map((d) => d.max_ms),
            backgroundColor: CHART_COLORS.warning + "88",
          },
        ],
      },
      options: { ...chartDefaults(), indexAxis: "y" },
    });
  }

  // Governor table
  const govTbody = document.querySelector("#governor-table tbody");
  if (govTbody && data.governor) {
    govTbody.innerHTML = data.governor
      .map(
        (g) => `<tr>
            <td>${fmtTime(g.time)}</td>
            <td>${g.governor_action || "—"}</td>
            <td>${fmt(g.ram_percent)}%</td>
            <td>${fmt(g.cpu_percent)}%</td>
        </tr>`,
      )
      .join("");
  }

  // Ingest runs table
  const runsTbody = document.querySelector("#ingest-runs-table tbody");
  if (runsTbody && data.runs) {
    runsTbody.innerHTML = data.runs
      .map(
        (r) => `<tr>
            <td>${fmtShortTime(r.timestamp)}</td>
            <td><span class="badge ${r.status === "ok" ? "badge-success" : "badge-error"}">${r.status}</span></td>
            <td>${fmtInt(r.files_parsed)}</td>
            <td>${fmtInt(r.chunks_stored)}</td>
            <td>${fmtInt(r.stale_deleted)}</td>
            <td>${r.error_count || 0}</td>
            <td>${r.duration_s != null ? fmt(r.duration_s) + "s" : "—"}</td>
        </tr>`,
      )
      .join("");
  }
}

// ─── Tab: CAG & Graph ───────────────────────────────────────────────────────

async function loadCAG() {
  const data = await fetchJson("/cag", { days: STATE.days });
  if (!data) return;

  const s = data.summary || {};
  setKpi("kpi-pack-hit-rate", fmtPct(s.hit_rate));
  setKpiClass(
    "kpi-pack-hit-rate",
    s.hit_rate > 50 ? "success" : s.hit_rate > 20 ? "warning" : "error",
  );
  const rcPct =
    s.response_total > 0 ? (s.response_hits / s.response_total) * 100 : 0;
  setKpi("kpi-response-cache", fmtPct(rcPct));
  setKpi("kpi-avg-nodes", fmt(s.avg_nodes));
  setKpi("kpi-avg-communities", fmt(s.avg_communities));

  if (data.timeline && data.timeline.length) {
    const labels = data.timeline.map((d) => fmtShortTime(d.time));
    makeOrUpdate("cag-timeline", "chart-cag-timeline", {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Hits",
            data: data.timeline.map((d) => d.hits),
            backgroundColor: CHART_COLORS.success + "aa",
          },
          {
            label: "Misses",
            data: data.timeline.map((d) => d.misses),
            backgroundColor: CHART_COLORS.error + "66",
          },
        ],
      },
      options: {
        ...chartDefaults(),
        scales: {
          ...chartDefaults().scales,
          x: { ...chartDefaults().scales.x, stacked: true },
          y: { ...chartDefaults().scales.y, stacked: true },
        },
      },
    });
  }

  if (data.pack_types && data.pack_types.length) {
    makeOrUpdate("pack-types", "chart-pack-types", {
      type: "doughnut",
      data: {
        labels: data.pack_types.map((d) => d.pack_type),
        datasets: [
          {
            data: data.pack_types.map((d) => d.value),
            backgroundColor: [
              CHART_COLORS.accent,
              CHART_COLORS.purple,
              CHART_COLORS.success,
              CHART_COLORS.warning,
            ],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: getComputedStyle(document.documentElement)
                .getPropertyValue("--text-secondary")
                .trim(),
              font: { size: 11 },
            },
          },
        },
      },
    });
  }
}

// ─── Tab: Infrastructure ────────────────────────────────────────────────────

async function loadInfra() {
  const data = await fetchJson("/resources", {
    hours: STATE.days * 24 > 168 ? 168 : Math.max(STATE.days * 24, 6),
  });
  if (!data) return;

  const c = data.current || {};
  setKpi("kpi-cpu", fmtPct(c.cpu_percent));
  setKpi("kpi-ram", fmtPct(c.ram_percent));
  setKpi("kpi-vram", fmtPct(c.vram_percent));
  setKpi(
    "kpi-disk",
    c.disk_free_gb != null ? fmt(c.disk_free_gb) + " GB" : "—",
  );

  setGauge("gauge-cpu", c.cpu_percent);
  setGauge("gauge-ram", c.ram_percent);
  setGauge("gauge-vram", c.vram_percent);

  if (data.store_latency && data.store_latency.length) {
    const avg =
      data.store_latency.reduce((a, d) => a + (d.query_ms || 0), 0) /
      data.store_latency.length;
    setKpi("kpi-store-latency", fmtMs(avg));
  }

  if (data.embedding_latency && data.embedding_latency.length) {
    const totalTexts = data.embedding_latency.reduce(
      (a, d) => a + (d.total_texts || 0),
      0,
    );
    const hours = Math.max(
      1,
      (Date.now() - new Date(data.embedding_latency[0].time).getTime()) /
        3600000,
    );
    setKpi("kpi-embed-rate", fmtInt(Math.round(totalTexts / hours)) + "/h");
  }

  if (data.history && data.history.length) {
    const labels = data.history.map((d) => fmtShortTime(d.time));
    makeOrUpdate("resources-history", "chart-resources-history", {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "CPU %",
            data: data.history.map((d) => d.cpu),
            borderColor: CHART_COLORS.accent,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: "RAM %",
            data: data.history.map((d) => d.ram),
            borderColor: CHART_COLORS.success,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: "VRAM %",
            data: data.history.map((d) => d.vram),
            borderColor: CHART_COLORS.purple,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
        ],
      },
      options: chartDefaults(),
    });
  }

  if (data.store_latency && data.store_latency.length) {
    const labels = data.store_latency.map((d) => fmtShortTime(d.time));
    makeOrUpdate("store-latency", "chart-store-latency", {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Query",
            data: data.store_latency.map((d) => d.query_ms),
            borderColor: CHART_COLORS.accent,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: "Upsert",
            data: data.store_latency.map((d) => d.upsert_ms),
            borderColor: CHART_COLORS.warning,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 0,
          },
        ],
      },
      options: chartDefaults(),
    });
  }
}

// ─── Tab: Live Events (SSE) ─────────────────────────────────────────────────

function connectSSE() {
  if (STATE.sse) {
    STATE.sse.close();
    STATE.sse = null;
  }
  const es = new EventSource(API + "/events");
  STATE.sse = es;
  STATE.eventStart = Date.now();
  STATE.eventCount = 0;
  STATE.eventErrors = 0;

  setKpi("kpi-sse-status", "connecting");

  es.onopen = () => setKpi("kpi-sse-status", "connected");
  es.onerror = () => {
    setKpi("kpi-sse-status", "error");
    setKpiClass("kpi-sse-status", "error");
  };
  es.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      if (evt.type === "connected") {
        setKpi("kpi-sse-status", "live");
        setKpiClass("kpi-sse-status", "success");
        return;
      }
      STATE.eventCount++;
      if (evt.success === false || evt.success === 0) STATE.eventErrors++;
      setKpi("kpi-events-count", STATE.eventCount);
      setKpi("kpi-events-errors", STATE.eventErrors);

      const elapsed = (Date.now() - STATE.eventStart) / 60000;
      setKpi(
        "kpi-events-rate",
        elapsed > 0.1 ? fmt(STATE.eventCount / elapsed) : "—",
      );

      appendEventRow(evt);
    } catch {
      /* ignore malformed */
    }
  };
}

function appendEventRow(evt) {
  const tbody = document.querySelector("#events-table tbody");
  if (!tbody) return;
  const tr = document.createElement("tr");
  const status = evt.success === false || evt.success === 0 ? "error" : "ok";
  tr.innerHTML = `
        <td>${fmtTime(evt.timestamp || new Date().toISOString())}</td>
        <td>${evt.event || evt.type || "—"}</td>
        <td>${evt.latency_ms != null ? fmtMs(evt.latency_ms) : "—"}</td>
        <td><span class="badge ${status === "ok" ? "badge-success" : "badge-error"}">${status}</span></td>
        <td>${evt.query_hash || evt.run_id || evt.collection || "—"}</td>
    `;
  tbody.prepend(tr);
  while (tbody.children.length > 200) tbody.lastChild.remove();
}

// ─── Health Badge ───────────────────────────────────────────────────────────

function updateHealthBadge(errorRate) {
  const badge = document.getElementById("health-badge");
  if (!badge) return;
  if (errorRate == null) {
    badge.classList.remove("error");
    return;
  }
  if (errorRate > 5) badge.classList.add("error");
  else badge.classList.remove("error");
}

// ─── Tab Router ─────────────────────────────────────────────────────────────

function switchTab(tabName) {
  $$(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === tabName),
  );
  $$(".tab-content").forEach((s) =>
    s.classList.toggle("active", s.id === "tab-" + tabName),
  );
  loadTabData(tabName);
}

async function loadTabData(tabName) {
  switch (tabName) {
    case "overview":
      return loadOverview();
    case "retrieval":
      return loadRetrieval();
    case "ingest":
      return loadIngest();
    case "cag":
      return loadCAG();
    case "infra":
      return loadInfra();
    case "live":
      if (!STATE.sse || STATE.sse.readyState === EventSource.CLOSED)
        connectSSE();
      return;
  }
}

function getActiveTab() {
  const active = $(".tab.active");
  return active ? active.dataset.tab : "overview";
}

// ─── Refresh ────────────────────────────────────────────────────────────────

function refreshCurrent() {
  loadTabData(getActiveTab());
  document.getElementById("last-updated").textContent =
    "Updated " + new Date().toLocaleTimeString();
}

function startAutoRefresh() {
  stopAutoRefresh();
  STATE.refreshInterval = setInterval(refreshCurrent, 30000);
}

function stopAutoRefresh() {
  if (STATE.refreshInterval) {
    clearInterval(STATE.refreshInterval);
    STATE.refreshInterval = null;
  }
}

// ─── Theme ──────────────────────────────────────────────────────────────────

function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  html.setAttribute("data-theme", next);
  localStorage.setItem("rag-dashboard-theme", next);
  destroyCharts();
  refreshCurrent();
}

function loadTheme() {
  const saved = localStorage.getItem("rag-dashboard-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
}

// ─── Init ───────────────────────────────────────────────────────────────────

function init() {
  loadTheme();

  // Tab clicks
  $$(".tab").forEach((btn) =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab)),
  );

  // Period selector
  $$(".period-btn").forEach((btn) =>
    btn.addEventListener("click", () => {
      $$(".period-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      STATE.days = parseInt(btn.dataset.days, 10);
      destroyCharts();
      refreshCurrent();
    }),
  );

  // Theme toggle
  const themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) themeBtn.addEventListener("click", toggleTheme);

  // Refresh toggle
  const refreshBtn = document.getElementById("refresh-toggle");
  if (refreshBtn)
    refreshBtn.addEventListener("click", () => {
      STATE.autoRefresh = !STATE.autoRefresh;
      refreshBtn.classList.toggle("active", STATE.autoRefresh);
      if (STATE.autoRefresh) startAutoRefresh();
      else stopAutoRefresh();
    });
  refreshBtn?.classList.add("active");

  // Initial load
  refreshCurrent();
  startAutoRefresh();
}

document.addEventListener("DOMContentLoaded", init);
