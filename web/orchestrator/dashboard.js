/**
 * AI Orchestrator Dashboard v1.1 — Production JavaScript
 * Centralized fetch, robust SSE, data normalization, rich Models/Backends/Events
 */
(function () {
  "use strict";

  // ═══════════════════════════════════════
  // STATE
  // ═══════════════════════════════════════
  const state = {
    period: 7,
    autoRefresh: true,
    refreshInterval: null,
    sseConnection: null,
    sseReconnectTimer: null,
    sseReconnectDelay: 1000,
    charts: {},
    sessionsPage: 0,
    sessionsLimit: 50,
    loadedTabs: new Set(),
    eventsCount: 0,
    eventsErrors: 0,
    modelsData: null,
    backendsData: null,
  };

  const API = "/dashboard";
  const MAX_EVENTS = 100;

  // ═══════════════════════════════════════
  // DOM HELPERS
  // ═══════════════════════════════════════
  function $(sel, ctx) {
    return (ctx || document).querySelector(sel);
  }
  function $$(sel, ctx) {
    return [...(ctx || document).querySelectorAll(sel)];
  }

  // ═══════════════════════════════════════
  // DATA NORMALIZATION HELPERS
  // ═══════════════════════════════════════
  function safeNumber(v, fallback) {
    const n = Number(v);
    return isNaN(n) ? fallback || 0 : n;
  }
  function safeArray(v) {
    return Array.isArray(v) ? v : [];
  }
  function safeString(v, fallback) {
    if (v == null) return fallback || "—";
    if (typeof v === "string") return v || fallback || "—";
    if (typeof v === "object" && v.name) return String(v.name);
    return String(v);
  }

  function entityName(val, maxLen) {
    if (!val) return "—";
    if (typeof val === "string") return truncate(val, maxLen || 20);
    if (typeof val === "object" && val.name)
      return truncate(String(val.name), maxLen || 20);
    if (typeof val === "object" && val.model_name)
      return truncate(String(val.model_name), maxLen || 20);
    if (typeof val === "object" && val.backend_name)
      return truncate(String(val.backend_name), maxLen || 20);
    return "—";
  }

  function formatTokens(n) {
    n = safeNumber(n);
    if (n === 0) return "0";
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
    if (n >= 10_000) return (n / 1_000).toFixed(1) + "K";
    if (n >= 1_000) return n.toLocaleString();
    return String(n);
  }

  function formatLatency(ms) {
    ms = safeNumber(ms);
    if (ms === 0) return "—";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + "s";
    return Math.round(ms) + "ms";
  }

  function formatPercent(v) {
    v = safeNumber(v);
    if (v === 0) return "0%";
    return v.toFixed(1) + "%";
  }

  function formatTime(ts) {
    if (!ts) return "—";
    let d;
    if (typeof ts === "number") {
      d = new Date(ts > 1e12 ? ts : ts * 1000);
    } else if (typeof ts === "string") {
      d = new Date(ts.includes("T") ? ts : ts + "T00:00:00");
    } else return "—";
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function formatDate(ts) {
    if (!ts) return "—";
    let d;
    if (typeof ts === "number") d = new Date(ts > 1e12 ? ts : ts * 1000);
    else if (typeof ts === "string") d = new Date(ts);
    else return "—";
    if (isNaN(d.getTime())) return "—";
    return (
      d.toLocaleDateString([], { month: "short", day: "numeric" }) +
      " " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    );
  }

  function truncate(str, len) {
    if (!str) return "—";
    str = String(str);
    return str.length > len ? str.slice(0, len) + "…" : str;
  }

  function normalizeModel(raw) {
    if (!raw || typeof raw !== "object") return null;
    return {
      name: safeString(raw.model_name || raw.display_name || raw.model),
      backends: safeArray(raw.backends),
      available: !!raw.available,
      configured: !!raw.configured,
      detected: !!raw.detected_runtime,
      used: !!raw.used_in_period,
      enabled: raw.enabled !== false,
      healthStatus: safeString(raw.health_status, "unknown"),
      calls: safeNumber(raw.total_calls),
      tokens: safeNumber(raw.total_tokens),
      promptTokens: safeNumber(raw.prompt_tokens),
      completionTokens: safeNumber(raw.completion_tokens),
      avgLatency: safeNumber(raw.avg_latency_ms),
      p95Latency: safeNumber(raw.p95_latency_ms),
      errorCount: safeNumber(raw.error_count),
      errorRate: safeNumber(raw.error_rate),
      tokensPerSec: safeNumber(raw.tokens_per_second),
      fallbackIn: safeNumber(raw.fallback_in_count),
      fallbackOut: safeNumber(raw.fallback_out_count),
      lastUsed: raw.last_used_at,
      usageSource: safeString(raw.usage_source, "missing"),
      privacyLevel: safeString(raw.privacy_level, "local"),
    };
  }

  function normalizeBackend(raw) {
    if (!raw || typeof raw !== "object") return null;
    return {
      name: safeString(raw.backend_name || raw.name),
      type: safeString(raw.backend_type, "openai_compatible"),
      baseUrl: safeString(raw.base_url, ""),
      privacyLevel: safeString(raw.privacy_level, "local"),
      enabled: raw.enabled !== false,
      healthStatus: safeString(raw.health_status, "unknown"),
      healthLatency: safeNumber(raw.health_latency_ms),
      lastError: raw.last_error || null,
      configuredModels: safeArray(raw.configured_models),
      detectedModels: safeArray(raw.detected_models),
      modelsCount: safeNumber(raw.available_models_count),
      priority: safeNumber(raw.priority, 99),
      calls: safeNumber(raw.total_calls),
      tokens: safeNumber(raw.total_tokens),
      promptTokens: safeNumber(raw.prompt_tokens),
      completionTokens: safeNumber(raw.completion_tokens),
      avgLatency: safeNumber(raw.avg_latency_ms),
      p95Latency: safeNumber(raw.p95_latency_ms),
      tokensPerSec: safeNumber(raw.tokens_per_second),
      errorCount: safeNumber(raw.error_count),
      errorRate: safeNumber(raw.error_rate),
      fallbackIn: safeNumber(raw.fallback_in_count),
      fallbackOut: safeNumber(raw.fallback_out_count),
      lastUsed: raw.last_used_at,
    };
  }

  function normalizeEvent(raw) {
    if (!raw || typeof raw !== "object") return null;
    return {
      type: safeString(raw.type, "unknown"),
      timestamp: raw.timestamp,
      model: safeString(raw.model),
      backend: safeString(raw.backend),
      tokens: safeNumber(raw.total_tokens),
      promptTokens: safeNumber(raw.prompt_tokens),
      completionTokens: safeNumber(raw.completion_tokens),
      latency: safeNumber(raw.latency_ms),
      success: raw.success !== false,
      errorType: raw.error_type || null,
      stream: !!raw.stream,
      agentic: !!raw.agentic,
      intent: raw.intent || null,
      sessionId: raw.session_id || null,
      fallbackUsed: !!raw.fallback_used,
      requestId: raw.request_id || null,
    };
  }

  // ═══════════════════════════════════════
  // CENTRALIZED API CLIENT
  // ═══════════════════════════════════════
  async function apiFetch(endpoint, params = {}) {
    const url = new URL(API + endpoint, window.location.origin);
    Object.entries(params).forEach(([k, v]) => {
      if (v != null) url.searchParams.set(k, v);
    });
    try {
      const res = await fetch(url);
      if (res.status === 401) {
        showTabError(
          null,
          "Authentication error (401). Dashboard routes should be auth-exempt.",
        );
        return { _error: true, status: 401, message: "Unauthorized" };
      }
      if (!res.ok) {
        return {
          _error: true,
          status: res.status,
          message: `HTTP ${res.status}`,
        };
      }
      const data = await res.json();
      return data;
    } catch (err) {
      console.error(`[API] ${endpoint}:`, err);
      return {
        _error: true,
        status: 0,
        message: err.message || "Network error",
      };
    }
  }

  function isError(data) {
    return data && data._error === true;
  }

  function showTabError(panelId, msg) {
    if (!panelId) return;
    const panel = document.getElementById(panelId);
    if (!panel) return;
    const existing = panel.querySelector(".tab-error-banner");
    if (existing) existing.remove();
    const banner = document.createElement("div");
    banner.className = "tab-error-banner";
    banner.innerHTML = `<span class="badge badge-error">Error</span> <span>${msg}</span>`;
    panel.insertBefore(banner, panel.firstChild);
    setTimeout(() => banner.remove(), 10000);
  }

  // ═══════════════════════════════════════
  // TAB ROUTING
  // ═══════════════════════════════════════
  function initTabs() {
    const tabs = $$(".tab");
    const panels = $$(".tab-panel");

    function activate(name) {
      tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
      panels.forEach((p) =>
        p.classList.toggle("active", p.id === "panel-" + name),
      );
    }

    tabs.forEach((t) =>
      t.addEventListener("click", (e) => {
        e.preventDefault();
        const name = t.dataset.tab;
        window.location.hash = name;
        activate(name);
        loadTabData(name);
      }),
    );

    const hash = window.location.hash.slice(1) || "overview";
    activate(hash);
    return hash;
  }

  // ═══════════════════════════════════════
  // CHART HELPERS
  // ═══════════════════════════════════════
  const COLORS = [
    "#58a6ff",
    "#3fb950",
    "#bc8cff",
    "#d29922",
    "#f85149",
    "#39d3ef",
    "#f778ba",
    "#2ea043",
    "#79b8ff",
    "#e3b341",
  ];

  function chartTextColor() {
    return (
      getComputedStyle(document.body)
        .getPropertyValue("--text-secondary")
        .trim() || "#8b949e"
    );
  }
  function chartGridColor() {
    return (
      getComputedStyle(document.body).getPropertyValue("--border").trim() ||
      "#1e2a3a"
    );
  }

  function baseOpts(showLegend) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: {
          display: !!showLegend,
          labels: {
            color: chartTextColor(),
            font: { size: 11 },
            boxWidth: 12,
            padding: 10,
          },
        },
        tooltip: {
          backgroundColor: "#1a2433",
          titleColor: "#e6edf3",
          bodyColor: "#8b949e",
          borderColor: "#2d4157",
          borderWidth: 1,
          cornerRadius: 6,
          padding: 8,
        },
      },
      scales: {
        x: {
          ticks: {
            color: chartTextColor(),
            font: { size: 10 },
            maxRotation: 45,
          },
          grid: { color: chartGridColor() + "30" },
          border: { color: chartGridColor() },
        },
        y: {
          ticks: { color: chartTextColor(), font: { size: 10 } },
          grid: { color: chartGridColor() + "30" },
          border: { color: chartGridColor() },
          beginAtZero: true,
        },
      },
    };
  }

  function doughnutOpts() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      plugins: {
        legend: {
          position: "right",
          labels: {
            color: chartTextColor(),
            font: { size: 11 },
            boxWidth: 10,
            padding: 8,
          },
        },
        tooltip: {
          backgroundColor: "#1a2433",
          titleColor: "#e6edf3",
          bodyColor: "#8b949e",
          borderColor: "#2d4157",
          borderWidth: 1,
          cornerRadius: 6,
          padding: 8,
        },
      },
    };
  }

  function destroyChart(id) {
    if (state.charts[id]) {
      state.charts[id].destroy();
      delete state.charts[id];
    }
  }

  function makeChart(id, type, data, options) {
    destroyChart(id);
    const el = document.getElementById(id);
    if (!el) return;
    state.charts[id] = new Chart(el, { type, data, options });
  }

  function showEmpty(canvasId, msg) {
    const el = document.getElementById(canvasId);
    if (!el) return;
    const parent = el.parentElement;
    parent.innerHTML = `<div class="empty-state" style="height:100%;display:flex;align-items:center;justify-content:center;flex-direction:column"><p>${msg}</p></div>`;
  }

  function renderKPIGrid(containerId, cards) {
    const grid = document.getElementById(containerId);
    if (!grid) return;
    grid.innerHTML = cards
      .map(
        (c) => `
      <div class="kpi-card">
        <div class="kpi-value ${c.cls || ""}">${c.value}</div>
        <div class="kpi-label">${c.label}</div>
      </div>
    `,
      )
      .join("");
  }

  // ═══════════════════════════════════════
  // OVERVIEW TAB
  // ═══════════════════════════════════════
  async function loadOverview() {
    const [summary, timeline] = await Promise.all([
      apiFetch("/summary", { days: state.period }),
      apiFetch("/timeline", {
        days: state.period,
        resolution: state.period <= 1 ? "hour" : "day",
      }),
    ]);

    if (isError(summary)) {
      renderKPIGrid("kpi-grid", [
        { value: "⚠", label: "Failed to load", cls: "error" },
      ]);
      return;
    }

    // KPIs
    renderKPIGrid("kpi-grid", [
      {
        value: formatTokens(summary.total_sessions),
        label: "Sessions",
        cls: "accent",
      },
      { value: formatTokens(summary.total_messages), label: "Messages" },
      { value: formatTokens(summary.total_requests), label: "LLM Requests" },
      {
        value: formatTokens(summary.total_tokens),
        label: "Total Tokens",
        cls: "accent",
      },
      { value: entityName(summary.top_model, 18), label: "Top Model" },
      { value: entityName(summary.top_backend, 16), label: "Top Backend" },
      {
        value: summary.avg_latency_ms
          ? formatLatency(summary.avg_latency_ms)
          : "—",
        label: "Avg Latency",
      },
      {
        value: formatPercent(summary.error_rate),
        label: "Error Rate",
        cls: safeNumber(summary.error_rate) > 5 ? "error" : "success",
      },
    ]);
    updateDataSources(summary.data_sources_used);

    // Timeline charts
    if (!isError(timeline) && timeline.data && timeline.data.length > 0) {
      const labels = timeline.data.map((d) => {
        const p = d.period || d.date || "";
        if (p.length === 10 && p[4] === "-") {
          return new Date(p + "T00:00:00").toLocaleDateString([], {
            month: "short",
            day: "numeric",
          });
        }
        return p;
      });
      const sessions = timeline.data.map((d) => safeNumber(d.sessions));
      const messages = timeline.data.map((d) => safeNumber(d.messages));
      const requests = timeline.data.map((d) => safeNumber(d.requests));
      const tokens = timeline.data.map((d) => safeNumber(d.tokens));

      makeChart(
        "chart-timeline",
        "line",
        {
          labels,
          datasets: [
            {
              label: "Sessions",
              data: sessions,
              borderColor: COLORS[0],
              backgroundColor: COLORS[0] + "18",
              tension: 0.3,
              fill: true,
              pointRadius: 2,
            },
            {
              label: "Messages",
              data: messages,
              borderColor: COLORS[1],
              backgroundColor: "transparent",
              tension: 0.3,
              pointRadius: 2,
            },
            {
              label: "Requests",
              data: requests,
              borderColor: COLORS[2],
              backgroundColor: "transparent",
              tension: 0.3,
              pointRadius: 2,
            },
          ],
        },
        baseOpts(true),
      );

      makeChart(
        "chart-sessions-day",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Sessions",
              data: sessions,
              backgroundColor: COLORS[0] + "80",
              borderColor: COLORS[0],
              borderWidth: 1,
              borderRadius: 4,
            },
            {
              label: "Messages",
              data: messages,
              backgroundColor: COLORS[1] + "60",
              borderColor: COLORS[1],
              borderWidth: 1,
              borderRadius: 4,
            },
          ],
        },
        baseOpts(true),
      );

      makeChart(
        "chart-tokens",
        "line",
        {
          labels,
          datasets: [
            {
              label: "Tokens",
              data: tokens,
              borderColor: COLORS[3],
              backgroundColor: COLORS[3] + "15",
              tension: 0.3,
              fill: true,
              pointRadius: 2,
            },
          ],
        },
        baseOpts(false),
      );

      const totalUser = Math.round((summary.total_messages || 0) / 2);
      const totalAssistant = (summary.total_messages || 0) - totalUser;
      if (summary.total_messages > 0) {
        makeChart(
          "chart-msg-dist",
          "doughnut",
          {
            labels: ["User", "Assistant"],
            datasets: [
              {
                data: [totalUser, totalAssistant],
                backgroundColor: [COLORS[0], COLORS[1]],
                borderWidth: 0,
              },
            ],
          },
          doughnutOpts(),
        );
      } else {
        showEmpty("chart-msg-dist", "No messages yet");
      }
    } else {
      showEmpty("chart-timeline", "No timeline data yet");
      showEmpty("chart-sessions-day", "No session data yet");
      showEmpty("chart-tokens", "No token data yet");
      showEmpty("chart-msg-dist", "No messages yet");
    }
  }

  // ═══════════════════════════════════════
  // MODELS TAB
  // ═══════════════════════════════════════
  async function loadModels() {
    const raw = await apiFetch("/models", { days: state.period });
    if (isError(raw)) {
      showTabError("panel-models", raw.message);
      return;
    }

    state.modelsData = raw;
    const models = safeArray(raw.data).map(normalizeModel).filter(Boolean);
    const summary = raw.summary || {};

    // KPI Cards
    renderKPIGrid("models-kpi-grid", [
      {
        value: String(safeNumber(summary.total_models)),
        label: "Total Models",
        cls: "accent",
      },
      {
        value: String(safeNumber(summary.available_models)),
        label: "Available",
        cls: "success",
      },
      {
        value: String(safeNumber(summary.used_models)),
        label: "Used (period)",
      },
      {
        value: entityName(summary.top_model_by_tokens, 16),
        label: "Top by Tokens",
      },
      {
        value: summary.fastest_model
          ? truncate(summary.fastest_model, 14)
          : "—",
        label: "Fastest",
      },
      {
        value: summary.highest_error_model
          ? truncate(summary.highest_error_model, 14)
          : "—",
        label: "Most Errors",
        cls: summary.highest_error_rate > 0 ? "error" : "",
      },
    ]);

    // Charts
    const chartModels = models
      .filter((m) => m.calls > 0 || m.tokens > 0)
      .slice(0, 10);
    if (chartModels.length > 0) {
      const labels = chartModels.map((m) => truncate(m.name, 14));
      makeChart(
        "chart-models-tokens",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Tokens",
              data: chartModels.map((m) => m.tokens),
              backgroundColor: COLORS.slice(0, chartModels.length).map(
                (c) => c + "80",
              ),
              borderColor: COLORS.slice(0, chartModels.length),
              borderWidth: 1,
              borderRadius: 4,
            },
          ],
        },
        baseOpts(false),
      );
      makeChart(
        "chart-models-calls",
        "doughnut",
        {
          labels,
          datasets: [
            {
              data: chartModels.map((m) => m.calls),
              backgroundColor: COLORS.slice(0, chartModels.length),
              borderWidth: 0,
            },
          ],
        },
        doughnutOpts(),
      );
      makeChart(
        "chart-models-latency",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Avg Latency (ms)",
              data: chartModels.map((m) => m.avgLatency),
              backgroundColor: COLORS.slice(0, chartModels.length).map(
                (c) => c + "70",
              ),
              borderRadius: 4,
            },
          ],
        },
        { ...baseOpts(false), indexAxis: "y" },
      );
      makeChart(
        "chart-models-errors",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Error Rate (%)",
              data: chartModels.map((m) => m.errorRate),
              backgroundColor: chartModels.map((m) =>
                m.errorRate > 5 ? COLORS[4] + "80" : COLORS[1] + "60",
              ),
              borderRadius: 4,
            },
          ],
        },
        baseOpts(false),
      );
    } else {
      showEmpty(
        "chart-models-tokens",
        "No usage data — showing configured models below",
      );
      showEmpty("chart-models-calls", "No call data yet");
      showEmpty("chart-models-latency", "No latency data yet");
      showEmpty("chart-models-errors", "No error data yet");
    }

    // Table
    renderModelsTable(models);
  }

  function renderModelsTable(models) {
    const search = ($("#models-search")?.value || "").toLowerCase();
    const filter = $("#models-filter-status")?.value || "";

    let filtered = models;
    if (search)
      filtered = filtered.filter((m) => m.name.toLowerCase().includes(search));
    if (filter === "available") filtered = filtered.filter((m) => m.available);
    if (filter === "configured")
      filtered = filtered.filter((m) => m.configured);
    if (filter === "used") filtered = filtered.filter((m) => m.used);
    if (filter === "offline") filtered = filtered.filter((m) => !m.available);

    const table = $("#models-table");
    if (filtered.length === 0) {
      table.innerHTML =
        '<div class="empty-state"><span class="empty-icon">📊</span><p>No models match filters.<br><small>Try changing the filter or period.</small></p></div>';
      return;
    }

    table.innerHTML = `<table>
      <thead><tr>
        <th>Model</th><th>Status</th><th>Backends</th><th>Calls</th>
        <th>Tokens</th><th>Avg Latency</th><th>tok/s</th><th>Errors</th><th>Source</th>
      </tr></thead>
      <tbody>${filtered
        .map((m) => {
          const badges = [];
          if (m.available)
            badges.push('<span class="badge badge-success">available</span>');
          else if (m.configured && m.enabled)
            badges.push('<span class="badge badge-warning">configured</span>');
          else if (!m.enabled)
            badges.push('<span class="badge badge-muted">disabled</span>');
          else badges.push('<span class="badge badge-error">offline</span>');
          if (m.detected)
            badges.push('<span class="badge badge-info">detected</span>');
          if (m.used)
            badges.push('<span class="badge badge-purple">used</span>');

          const srcBadge =
            m.usageSource === "backend"
              ? '<span class="badge badge-info">real</span>'
              : m.usageSource === "estimated"
                ? '<span class="badge badge-warning">estimated</span>'
                : '<span class="badge badge-muted">—</span>';

          return `<tr>
          <td class="mono">${truncate(m.name, 24)}</td>
          <td>${badges.join(" ")}</td>
          <td class="text-sm">${m.backends.length > 0 ? m.backends.join(", ") : "—"}</td>
          <td>${formatTokens(m.calls)}</td>
          <td>${formatTokens(m.tokens)}</td>
          <td>${formatLatency(m.avgLatency)}</td>
          <td>${m.tokensPerSec > 0 ? m.tokensPerSec.toFixed(0) : "—"}</td>
          <td>${m.errorCount > 0 ? '<span class="text-error">' + m.errorCount + "</span> (" + formatPercent(m.errorRate) + ")" : "0"}</td>
          <td>${srcBadge}</td>
        </tr>`;
        })
        .join("")}</tbody>
    </table>`;
  }

  // ═══════════════════════════════════════
  // BACKENDS TAB
  // ═══════════════════════════════════════
  async function loadBackends() {
    const raw = await apiFetch("/backends", { days: state.period });
    if (isError(raw)) {
      showTabError("panel-backends", raw.message);
      return;
    }

    state.backendsData = raw;
    const backends = safeArray(raw.data).map(normalizeBackend).filter(Boolean);
    const summary = raw.summary || {};

    // KPI Cards
    renderKPIGrid("backends-kpi-grid", [
      {
        value: String(safeNumber(summary.total_backends)),
        label: "Total Backends",
        cls: "accent",
      },
      {
        value: String(safeNumber(summary.healthy_backends)),
        label: "Healthy",
        cls: "success",
      },
      {
        value: String(safeNumber(summary.degraded_backends)),
        label: "Degraded",
        cls: safeNumber(summary.degraded_backends) > 0 ? "warning" : "",
      },
      {
        value: String(safeNumber(summary.offline_backends)),
        label: "Offline/Disabled",
        cls: safeNumber(summary.offline_backends) > 0 ? "error" : "",
      },
      { value: entityName(summary.most_used_backend, 14), label: "Most Used" },
      { value: entityName(summary.fastest_backend, 14), label: "Fastest" },
    ]);

    // Charts
    const activeBackends = backends.filter((b) => b.calls > 0 || b.enabled);
    if (activeBackends.length > 0) {
      const labels = activeBackends.map((b) => truncate(b.name, 14));
      makeChart(
        "chart-backends-requests",
        "doughnut",
        {
          labels,
          datasets: [
            {
              data: activeBackends.map((b) => Math.max(b.calls, 0)),
              backgroundColor: COLORS.slice(0, activeBackends.length),
              borderWidth: 0,
            },
          ],
        },
        doughnutOpts(),
      );
      makeChart(
        "chart-backends-tokens",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Tokens",
              data: activeBackends.map((b) => b.tokens),
              backgroundColor: COLORS.slice(0, activeBackends.length).map(
                (c) => c + "70",
              ),
              borderRadius: 4,
            },
          ],
        },
        baseOpts(false),
      );
      makeChart(
        "chart-backends-latency",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Health Probe (ms)",
              data: activeBackends.map((b) => b.healthLatency || 0),
              backgroundColor: COLORS[0] + "60",
              borderRadius: 4,
            },
            {
              label: "Avg Call (ms)",
              data: activeBackends.map((b) => b.avgLatency),
              backgroundColor: COLORS[2] + "60",
              borderRadius: 4,
            },
          ],
        },
        baseOpts(true),
      );
      makeChart(
        "chart-backends-errors",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Errors",
              data: activeBackends.map((b) => b.errorCount),
              backgroundColor: COLORS[4] + "70",
              borderRadius: 4,
            },
            {
              label: "Fallback In",
              data: activeBackends.map((b) => b.fallbackIn),
              backgroundColor: COLORS[3] + "70",
              borderRadius: 4,
            },
          ],
        },
        baseOpts(true),
      );
    } else {
      showEmpty("chart-backends-requests", "No backend data");
      showEmpty("chart-backends-tokens", "No token data");
      showEmpty("chart-backends-latency", "No latency data");
      showEmpty("chart-backends-errors", "No error data");
    }

    // Table
    const table = $("#backends-table");
    if (backends.length === 0) {
      table.innerHTML =
        '<div class="empty-state"><span class="empty-icon">🖧</span><p>No backends configured.</p></div>';
      return;
    }

    table.innerHTML = `<table>
      <thead><tr>
        <th>Backend</th><th>Health</th><th>URL</th><th>Privacy</th>
        <th>Models</th><th>Calls</th><th>Tokens</th><th>Latency</th><th>Errors</th><th>Fallback</th>
      </tr></thead>
      <tbody>${backends
        .map((b) => {
          const healthBadge =
            b.healthStatus === "healthy"
              ? '<span class="badge badge-success">healthy</span>'
              : b.healthStatus === "disabled"
                ? '<span class="badge badge-muted">disabled</span>'
                : b.healthStatus === "unavailable"
                  ? '<span class="badge badge-error">unavailable</span>'
                  : b.healthStatus === "configured"
                    ? '<span class="badge badge-warning">configured</span>'
                    : `<span class="badge badge-outline">${b.healthStatus}</span>`;
          const privacyBadge =
            b.privacyLevel === "local"
              ? '<span class="badge badge-success">local</span>'
              : b.privacyLevel === "lan"
                ? '<span class="badge badge-info">lan</span>'
                : '<span class="badge badge-warning">remote</span>';
          const modelsCount =
            b.detectedModels.length || b.configuredModels.length;
          const modelsText =
            modelsCount > 0
              ? `${modelsCount} model${modelsCount > 1 ? "s" : ""}`
              : "—";

          return `<tr>
          <td class="mono">${truncate(b.name, 16)}</td>
          <td>${healthBadge}</td>
          <td class="mono text-sm">${truncate(b.baseUrl, 28)}</td>
          <td>${privacyBadge}</td>
          <td title="${[...b.configuredModels, ...b.detectedModels].join(", ")}">${modelsText}</td>
          <td>${formatTokens(b.calls)}</td>
          <td>${formatTokens(b.tokens)}</td>
          <td>${b.healthLatency > 0 ? formatLatency(b.healthLatency) + ' <small class="text-muted">(probe)</small>' : formatLatency(b.avgLatency)}</td>
          <td>${b.errorCount > 0 ? '<span class="text-error">' + b.errorCount + "</span>" : "0"}</td>
          <td>${b.fallbackIn + b.fallbackOut > 0 ? "↓" + b.fallbackIn + " ↑" + b.fallbackOut : "—"}</td>
        </tr>`;
        })
        .join("")}</tbody>
    </table>`;
    if (backends.some((b) => b.lastError)) {
      const errBackends = backends.filter((b) => b.lastError);
      table.innerHTML += `<div class="backend-errors"><h4 class="text-sm text-error">Backend Errors:</h4>${errBackends.map((b) => `<div class="text-sm mono"><strong>${b.name}:</strong> ${truncate(b.lastError, 80)}</div>`).join("")}</div>`;
    }
  }

  // ═══════════════════════════════════════
  // SESSIONS TAB
  // ═══════════════════════════════════════
  async function loadSessions() {
    const search = ($("#session-search")?.value || "").trim();
    const [sessionsData, timeline] = await Promise.all([
      apiFetch("/sessions", {
        days: state.period,
        limit: state.sessionsLimit,
        offset: state.sessionsPage * state.sessionsLimit,
      }),
      apiFetch("/timeline", { days: state.period, resolution: "day" }),
    ]);

    // Activity chart
    if (!isError(timeline) && timeline.data && timeline.data.length > 0) {
      const labels = timeline.data.map((d) => {
        const p = d.period || d.date || "";
        if (p.length === 10 && p[4] === "-")
          return new Date(p + "T00:00:00").toLocaleDateString([], {
            month: "short",
            day: "numeric",
          });
        return p;
      });
      makeChart(
        "chart-sessions-activity",
        "bar",
        {
          labels,
          datasets: [
            {
              label: "Sessions",
              data: timeline.data.map((d) => safeNumber(d.sessions)),
              backgroundColor: COLORS[0] + "70",
              borderColor: COLORS[0],
              borderWidth: 1,
              borderRadius: 3,
            },
          ],
        },
        baseOpts(false),
      );
    }

    // Table
    const table = $("#sessions-table");
    if (
      isError(sessionsData) ||
      !sessionsData.data ||
      sessionsData.data.length === 0
    ) {
      table.innerHTML =
        '<div class="empty-state"><span class="empty-icon">💬</span><p>No sessions in this period.</p></div>';
      $("#sessions-pagination").innerHTML = "";
      return;
    }

    let rows = sessionsData.data;
    if (search)
      rows = rows.filter((r) =>
        (r.session_id || "").toLowerCase().includes(search.toLowerCase()),
      );

    table.innerHTML = `<table>
      <thead><tr><th>Session ID</th><th>Messages</th><th>Started</th><th>Last Activity</th><th>Duration</th></tr></thead>
      <tbody>${rows
        .map((r) => {
          const started = r.started_at || r.first_message_at;
          const last = r.last_activity_at || r.last_message_at;
          const dur =
            started && last && last > started
              ? Math.round((last - started) / 60)
              : null;
          return `<tr class="clickable-row" data-sid="${safeString(r.session_id)}">
          <td class="mono">${truncate(safeString(r.session_id), 20)}</td>
          <td>${safeNumber(r.message_count)}</td>
          <td>${formatDate(started)}</td>
          <td>${formatDate(last)}</td>
          <td>${dur != null ? dur + " min" : "—"}</td>
        </tr>`;
        })
        .join("")}</tbody>
    </table>`;

    // Pagination
    const total = sessionsData.total || 0;
    const pages = Math.ceil(total / state.sessionsLimit);
    const pag = $("#sessions-pagination");
    if (pages > 1) {
      const cur = state.sessionsPage;
      let btns = "";
      if (cur > 0)
        btns += `<button class="btn btn-sm" data-p="${cur - 1}">‹</button>`;
      for (let i = Math.max(0, cur - 2); i < Math.min(pages, cur + 3); i++) {
        btns += `<button class="btn btn-sm ${i === cur ? "active" : ""}" data-p="${i}">${i + 1}</button>`;
      }
      if (cur < pages - 1)
        btns += `<button class="btn btn-sm" data-p="${cur + 1}">›</button>`;
      pag.innerHTML = btns;
      pag.querySelectorAll("[data-p]").forEach((b) =>
        b.addEventListener("click", () => {
          state.sessionsPage = +b.dataset.p;
          loadSessions();
        }),
      );
    } else {
      pag.innerHTML = "";
    }

    table
      .querySelectorAll(".clickable-row")
      .forEach((row) =>
        row.addEventListener("click", () => showSessionDetail(row.dataset.sid)),
      );
  }

  async function showSessionDetail(sid) {
    const modal = $("#session-modal");
    const content = $("#session-detail-content");
    modal.style.display = "flex";
    content.innerHTML = '<div class="empty-state">Loading...</div>';

    const data = await apiFetch(`/session/${encodeURIComponent(sid)}`);
    if (isError(data)) {
      content.innerHTML =
        '<div class="empty-state error-state">Failed to load session</div>';
      return;
    }

    let html = `<div style="margin-bottom:1rem">
      <p class="text-sm text-muted"><strong>ID:</strong> <span class="mono">${safeString(data.session_id)}</span></p>
      <p class="text-sm text-muted"><strong>Messages:</strong> ${safeNumber(data.message_count)}</p>
    </div>`;

    if (data.messages && data.messages.length > 0) {
      html += '<div class="session-timeline">';
      data.messages.forEach((m) => {
        html += `<div class="session-msg role-${safeString(m.role, "unknown")}">
          <div class="session-msg-role">${safeString(m.role)}</div>
          <div class="session-msg-meta">${formatTime(m.timestamp || m.created_at)} — ${safeNumber(m.content_length)} chars</div>
        </div>`;
      });
      html += "</div>";
    }
    content.innerHTML = html;
  }

  // ═══════════════════════════════════════
  // PERFORMANCE TAB
  // ═══════════════════════════════════════
  async function loadPerformance() {
    const [perf, summary] = await Promise.all([
      apiFetch("/performance", { days: state.period }),
      apiFetch("/summary", { days: state.period }),
    ]);

    if (isError(perf) || !perf.data || Object.keys(perf.data).length === 0) {
      renderKPIGrid("perf-kpi-grid", [
        { value: "—", label: "No performance data yet" },
      ]);
      showEmpty("chart-latency-dist", "No latency data");
      showEmpty("chart-success-errors", "No data");
      return;
    }

    const d = perf.data;
    renderKPIGrid("perf-kpi-grid", [
      {
        value: formatLatency(d.p50_ms || d.p50_latency_ms),
        label: "P50 Latency",
      },
      {
        value: formatLatency(d.p95_ms || d.p95_latency_ms),
        label: "P95 Latency",
        cls: safeNumber(d.p95_ms || d.p95_latency_ms) > 5000 ? "warning" : "",
      },
      {
        value: formatLatency(d.p99_ms || d.p99_latency_ms),
        label: "P99 Latency",
      },
      { value: formatLatency(d.avg_ms || d.avg_latency_ms), label: "Average" },
      {
        value: formatTokens(
          d.total_requests ||
            (summary && !isError(summary) ? summary.total_requests : 0),
        ),
        label: "Total Requests",
        cls: "accent",
      },
    ]);

    if (d.latency_buckets && d.latency_buckets.length > 0) {
      makeChart(
        "chart-latency-dist",
        "bar",
        {
          labels: d.latency_buckets.map((b) => b.range || b.bucket),
          datasets: [
            {
              label: "Requests",
              data: d.latency_buckets.map((b) => b.count),
              backgroundColor: COLORS[0] + "70",
              borderColor: COLORS[0],
              borderWidth: 1,
              borderRadius: 4,
            },
          ],
        },
        baseOpts(false),
      );
    } else {
      showEmpty("chart-latency-dist", "No latency buckets");
    }

    const totalReq =
      !isError(summary) && summary ? safeNumber(summary.total_requests) : 0;
    const errors =
      !isError(summary) && summary ? safeNumber(summary.error_count) : 0;
    const success = totalReq - errors;
    if (totalReq > 0) {
      makeChart(
        "chart-success-errors",
        "doughnut",
        {
          labels: ["Success", "Errors"],
          datasets: [
            {
              data: [success, errors],
              backgroundColor: [COLORS[1], COLORS[4]],
              borderWidth: 0,
            },
          ],
        },
        doughnutOpts(),
      );
    } else {
      showEmpty("chart-success-errors", "No request data");
    }

    // Load detailed performance data
    await loadPerformanceDetailed();
  }

  async function loadPerformanceDetailed() {
    const detailed = await apiFetch("/performance/detailed", {
      days: state.period,
    });
    if (isError(detailed)) return;

    // KPI cards for detailed metrics
    const kpis = [];
    if (detailed.cold_start_rate != null) {
      kpis.push({
        value: (detailed.cold_start_rate * 100).toFixed(1) + "%",
        label: "Cold Start Rate",
        cls: detailed.cold_start_rate > 0.3 ? "warning" : "",
      });
    }
    if (detailed.avg_model_load_ms) {
      kpis.push({
        value: formatLatency(detailed.avg_model_load_ms),
        label: "Avg Model Load",
        cls: detailed.avg_model_load_ms > 1000 ? "warning" : "",
      });
    }
    if (detailed.avg_prompt_eval_ms) {
      kpis.push({
        value: formatLatency(detailed.avg_prompt_eval_ms),
        label: "Avg Prompt Eval",
      });
    }
    if (detailed.avg_generation_ms) {
      kpis.push({
        value: formatLatency(detailed.avg_generation_ms),
        label: "Avg Generation",
      });
    }
    if (detailed.avg_context_build_ms) {
      kpis.push({
        value: formatLatency(detailed.avg_context_build_ms),
        label: "Avg Context Build",
      });
    }
    if (detailed.avg_generation_tps) {
      kpis.push({
        value: detailed.avg_generation_tps.toFixed(1) + " t/s",
        label: "Avg Gen Tokens/s",
        cls: "accent",
      });
    }
    if (detailed.avg_prompt_tps) {
      kpis.push({
        value: detailed.avg_prompt_tps.toFixed(1) + " t/s",
        label: "Avg Prompt Tokens/s",
      });
    }
    if (kpis.length > 0) {
      renderKPIGrid("perf-detailed-kpi", kpis);
    }

    // Model breakdown table
    const tbody = document.getElementById("perf-model-tbody");
    if (tbody && detailed.model_breakdown) {
      tbody.innerHTML = "";
      const warmModels = (detailed.models_status || [])
        .filter((m) => m.warm)
        .map((m) => m.model);

      for (const m of detailed.model_breakdown) {
        const isWarm = warmModels.some(
          (w) => w === m.model || m.model.includes(w) || w.includes(m.model),
        );
        const statusBadge = isWarm
          ? '<span class="badge badge-success">WARM</span>'
          : '<span class="badge badge-danger">COLD</span>';
        const coldPct = (m.cold_start_rate * 100).toFixed(0) + "%";
        tbody.innerHTML += `<tr>
          <td><strong>${m.model}</strong></td>
          <td>${statusBadge}</td>
          <td>${m.queries}</td>
          <td>${formatLatency(m.avg_load_ms)}</td>
          <td>${formatLatency(m.avg_prompt_eval_ms)}</td>
          <td>${formatLatency(m.avg_generation_ms)}</td>
          <td>${formatLatency(m.avg_first_token_ms)}</td>
          <td>${m.avg_generation_tps ? m.avg_generation_tps.toFixed(1) : "—"}</td>
          <td>${coldPct}</td>
        </tr>`;
      }

      // Stacked latency breakdown chart
      if (detailed.model_breakdown.length > 0) {
        const labels = detailed.model_breakdown.map(
          (m) => m.model.split(":")[0],
        );
        makeChart(
          "chart-latency-breakdown",
          "bar",
          {
            labels,
            datasets: [
              {
                label: "Load",
                data: detailed.model_breakdown.map((m) => m.avg_load_ms || 0),
                backgroundColor: COLORS[0] + "90",
              },
              {
                label: "Prompt Eval",
                data: detailed.model_breakdown.map(
                  (m) => m.avg_prompt_eval_ms || 0,
                ),
                backgroundColor: COLORS[1] + "90",
              },
              {
                label: "Generation",
                data: detailed.model_breakdown.map(
                  (m) => m.avg_generation_ms || 0,
                ),
                backgroundColor: COLORS[2] + "90",
              },
            ],
          },
          {
            ...baseOpts(true),
            scales: {
              x: { stacked: true },
              y: { stacked: true, title: { display: true, text: "ms" } },
            },
          },
        );

        // Tokens/sec chart
        makeChart(
          "chart-tokens-per-sec",
          "bar",
          {
            labels,
            datasets: [
              {
                label: "Prompt tok/s",
                data: detailed.model_breakdown.map(
                  (m) => m.avg_prompt_tps || 0,
                ),
                backgroundColor: COLORS[3] + "80",
                borderRadius: 4,
              },
              {
                label: "Gen tok/s",
                data: detailed.model_breakdown.map(
                  (m) => m.avg_generation_tps || 0,
                ),
                backgroundColor: COLORS[5] + "80",
                borderRadius: 4,
              },
            ],
          },
          baseOpts(true),
        );
      }
    }
  }

  // ═══════════════════════════════════════
  // RESOURCES TAB
  // ═══════════════════════════════════════
  function renderGauge(containerId, percent, usedLabel, totalLabel, color) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const radius = 58;
    const circumference = 2 * Math.PI * radius;
    const offset = circumference - (percent / 100) * circumference;
    const strokeColor =
      percent > 90
        ? "var(--error)"
        : percent > 70
          ? "var(--warning)"
          : color || "var(--accent)";

    container.innerHTML = `
      <div class="gauge-ring">
        <svg viewBox="0 0 140 140">
          <circle class="gauge-bg" cx="70" cy="70" r="${radius}"/>
          <circle class="gauge-fill" cx="70" cy="70" r="${radius}"
            stroke="${strokeColor}"
            stroke-dasharray="${circumference}"
            stroke-dashoffset="${offset}"/>
        </svg>
        <div class="gauge-center">
          <div class="gauge-value">${percent.toFixed(1)}%</div>
          <div class="gauge-sub">${usedLabel}</div>
        </div>
      </div>
      <div class="gauge-label">${totalLabel}</div>
    `;
  }

  async function loadResources() {
    const [res, history] = await Promise.all([
      apiFetch("/resources"),
      apiFetch("/resources/history", { hours: 6 }),
    ]);

    if (isError(res) || !res.snapshot) {
      renderKPIGrid("resources-kpi-grid", [
        { value: "—", label: "No resource data yet" },
      ]);
      return;
    }

    const s = res.snapshot;

    // KPI cards
    const kpis = [];
    if (s.gpu_name) {
      kpis.push({ value: s.gpu_name, label: "GPU", cls: "accent" });
    }
    if (s.gpu_temperature_c != null) {
      kpis.push({
        value: s.gpu_temperature_c + "°C",
        label: "GPU Temp",
        cls: s.gpu_temperature_c > 80 ? "warning" : "",
      });
    }
    if (s.gpu_power_w != null) {
      kpis.push({ value: s.gpu_power_w.toFixed(0) + "W", label: "GPU Power" });
    }
    if (s.ollama_models_loaded != null) {
      kpis.push({
        value: String(s.ollama_models_loaded),
        label: "Models Loaded",
      });
    }
    if (s.cpu_count) {
      kpis.push({ value: String(s.cpu_count) + " cores", label: "CPU" });
    }
    renderKPIGrid("resources-kpi-grid", kpis);

    // VRAM gauge
    if (s.gpu_vram_total_mb && s.gpu_vram_used_mb != null) {
      const pct = (s.gpu_vram_used_mb / s.gpu_vram_total_mb) * 100;
      renderGauge(
        "gauge-vram",
        pct,
        `${(s.gpu_vram_used_mb / 1024).toFixed(1)} / ${(s.gpu_vram_total_mb / 1024).toFixed(1)} GB`,
        `${s.gpu_vram_free_mb || 0} MB free`,
        "var(--purple)",
      );
    } else {
      document.getElementById("gauge-vram").innerHTML =
        '<span class="text-muted">No GPU detected</span>';
    }

    // RAM gauge
    if (s.ram_total_mb && s.ram_percent != null) {
      renderGauge(
        "gauge-ram",
        s.ram_percent,
        `${(s.ram_used_mb / 1024).toFixed(1)} / ${(s.ram_total_mb / 1024).toFixed(1)} GB`,
        `${((s.ram_available_mb || 0) / 1024).toFixed(1)} GB available`,
        "var(--accent)",
      );
    }

    // CPU gauge
    if (s.cpu_percent != null) {
      renderGauge(
        "gauge-cpu",
        s.cpu_percent,
        `${s.cpu_percent}% usage`,
        `${s.cpu_count || "?"} threads`,
        "var(--success)",
      );
    }

    // Loaded models table
    const tbody = document.getElementById("resources-models-tbody");
    if (tbody) {
      const models = s.models_loaded || [];
      if (models.length === 0) {
        tbody.innerHTML =
          '<tr><td colspan="4" class="text-muted">No models loaded</td></tr>';
      } else {
        tbody.innerHTML = models
          .map(
            (m) => `<tr>
          <td><strong>${m.model}</strong></td>
          <td>${m.size_vram_mb || "—"}</td>
          <td>${m.size_mb || "—"}</td>
          <td>${m.expires_at ? new Date(m.expires_at).toLocaleTimeString() : "—"}</td>
        </tr>`,
          )
          .join("");
      }
    }

    // History charts
    if (!isError(history) && history.data && history.data.length > 1) {
      const timestamps = history.data.map((h) =>
        new Date(h.timestamp * 1000).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
      );

      // VRAM history
      if (history.data[0].gpu_vram_used_mb != null) {
        makeChart(
          "chart-vram-history",
          "line",
          {
            labels: timestamps,
            datasets: [
              {
                label: "VRAM Used (MB)",
                data: history.data.map((h) => h.gpu_vram_used_mb || 0),
                borderColor: COLORS[4],
                backgroundColor: COLORS[4] + "20",
                fill: true,
                tension: 0.3,
              },
              {
                label: "Ollama VRAM (MB)",
                data: history.data.map((h) => h.ollama_vram_used_mb || 0),
                borderColor: COLORS[5],
                backgroundColor: COLORS[5] + "20",
                fill: true,
                tension: 0.3,
              },
            ],
          },
          baseOpts(true),
        );
      } else {
        showEmpty("chart-vram-history", "No GPU data");
      }

      // RAM history
      makeChart(
        "chart-ram-history",
        "line",
        {
          labels: timestamps,
          datasets: [
            {
              label: "RAM Used (MB)",
              data: history.data.map((h) => h.ram_used_mb || 0),
              borderColor: COLORS[0],
              backgroundColor: COLORS[0] + "20",
              fill: true,
              tension: 0.3,
            },
          ],
        },
        baseOpts(false),
      );
    } else {
      showEmpty("chart-vram-history", "Collecting data...");
      showEmpty("chart-ram-history", "Collecting data...");
    }
  }

  // ═══════════════════════════════════════
  // LIVE EVENTS (SSE)
  // ═══════════════════════════════════════
  function initSSE() {
    // Close existing connection
    if (state.sseConnection) {
      state.sseConnection.close();
      state.sseConnection = null;
    }
    if (state.sseReconnectTimer) {
      clearTimeout(state.sseReconnectTimer);
      state.sseReconnectTimer = null;
    }

    const badge = $("#sse-status");
    const lastEvt = $("#sse-last-event");
    badge.className = "badge badge-warning";
    badge.textContent = "Connecting...";

    const src = new EventSource(API + "/events");
    state.sseConnection = src;

    src.onopen = () => {
      badge.className = "badge badge-success";
      badge.textContent = "Connected";
      // Only reset backoff after connection stays open for a bit
      setTimeout(() => {
        if (
          state.sseConnection === src &&
          src.readyState === EventSource.OPEN
        ) {
          state.sseReconnectDelay = 1000;
        }
      }, 5000);
    };

    src.onerror = () => {
      badge.className = "badge badge-error";
      badge.textContent = "Disconnected";
      src.close();
      state.sseConnection = null;
      // Reconnect with exponential backoff (max 30s)
      state.sseReconnectDelay = Math.min(
        (state.sseReconnectDelay || 1000) * 2,
        30000,
      );
      badge.textContent = `Reconnecting in ${Math.round(state.sseReconnectDelay / 1000)}s...`;
      state.sseReconnectTimer = setTimeout(initSSE, state.sseReconnectDelay);
    };

    src.onmessage = (e) => {
      try {
        const raw = JSON.parse(e.data);
        if (!raw || !raw.type) return;
        if (raw.type === "dashboard_ping") {
          lastEvt.textContent = "Ping " + formatTime(raw.timestamp);
          return;
        }
        if (raw.type === "connected") {
          lastEvt.textContent = "Connected";
          return;
        }
        const evt = normalizeEvent(raw);
        if (evt) addEventRow(evt);
      } catch (_) {}
    };
  }

  function addEventRow(evt) {
    const tbody = $("#events-tbody");
    const emptyEl = $("#events-empty");
    if (!tbody) return;

    // Hide empty state
    if (emptyEl) emptyEl.style.display = "none";

    state.eventsCount++;
    if (!evt.success) state.eventsErrors++;

    // Update last event time
    const lastEvt = $("#sse-last-event");
    if (lastEvt) lastEvt.textContent = "Last: " + formatTime(evt.timestamp);

    // Update stats
    const stats = $("#events-stats");
    if (stats) {
      stats.innerHTML =
        `<span class="badge badge-outline">Events: ${state.eventsCount}</span>` +
        (state.eventsErrors > 0
          ? ` <span class="badge badge-error">Errors: ${state.eventsErrors}</span>`
          : "");
    }

    // Status & type badges
    const statusBadge = evt.success
      ? '<span class="badge badge-success">success</span>'
      : '<span class="badge badge-error">error</span>';
    const typeBadge =
      evt.type === "llm_call_completed"
        ? '<span class="badge badge-info">llm_call</span>'
        : `<span class="badge badge-outline">${truncate(evt.type, 12)}</span>`;

    const flags = [
      evt.stream ? '<span class="badge badge-info">stream</span>' : "",
      evt.agentic ? '<span class="badge badge-purple">agentic</span>' : "",
      evt.fallbackUsed
        ? '<span class="badge badge-warning">fallback</span>'
        : "",
      evt.intent
        ? `<span class="badge badge-outline">${evt.intent}</span>`
        : "",
    ]
      .filter(Boolean)
      .join(" ");

    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${formatTime(evt.timestamp)}</td>
      <td>${typeBadge}</td>
      <td class="mono">${truncate(evt.model, 16)}</td>
      <td class="mono">${truncate(evt.backend, 12)}</td>
      <td>${evt.tokens > 0 ? formatTokens(evt.tokens) : "—"}</td>
      <td>${formatLatency(evt.latency)}</td>
      <td>${statusBadge}</td>
      <td>${flags || "—"}</td>
    `;
    tbody.insertBefore(row, tbody.firstChild);

    // Limit to MAX_EVENTS
    while (tbody.children.length > MAX_EVENTS)
      tbody.removeChild(tbody.lastChild);
  }

  // ═══════════════════════════════════════
  // DATA SOURCES BANNER
  // ═══════════════════════════════════════
  function updateDataSources(sources) {
    const banner = $("#data-source-banner");
    if (!sources || sources.length === 0) {
      banner.style.display = "none";
      return;
    }
    banner.style.display = "flex";
    banner.innerHTML =
      "Data from: " +
      sources
        .map((s) => {
          const cls = s.includes("sessions")
            ? "badge-info"
            : s.includes("metrics")
              ? "badge-purple"
              : s.includes("config")
                ? "badge-outline"
                : "badge-warning";
          return `<span class="badge ${cls}">${s}</span>`;
        })
        .join(" ");
  }

  // ═══════════════════════════════════════
  // HEALTH BADGE
  // ═══════════════════════════════════════
  async function updateHealth() {
    const badge = $("#health-badge");
    const diag = await apiFetch("/diagnostics");
    if (isError(diag)) {
      badge.className = "badge badge-error";
      badge.textContent = "Unreachable";
      return;
    }
    const sOk = diag.sessions_db?.available;
    const mOk = diag.metrics_db?.available;
    const bHealthy = diag.backends_healthy || 0;
    const bTotal = diag.backends_total || 0;

    if (sOk && mOk && bHealthy > 0) {
      badge.className = "badge badge-success";
      badge.textContent = `All Sources (${bHealthy}/${bTotal} backends)`;
    } else if (sOk || mOk) {
      badge.className = "badge badge-warning";
      badge.textContent = `Partial Data${bHealthy > 0 ? " (" + bHealthy + " backends)" : ""}`;
    } else {
      badge.className = "badge badge-error";
      badge.textContent = "No Data";
    }
  }

  // ═══════════════════════════════════════
  // TAB LOADING
  // ═══════════════════════════════════════
  async function loadTabData(tab) {
    switch (tab) {
      case "overview":
        await loadOverview();
        break;
      case "models":
        await loadModels();
        break;
      case "backends":
        await loadBackends();
        break;
      case "resources":
        await loadResources();
        break;
      case "sessions":
        await loadSessions();
        break;
      case "performance":
        await loadPerformance();
        break;
      case "graph":
        await loadGraphTracing();
        break;
      case "events":
        break; // SSE handles it
      case "gemilyni":
        await loadGemilyni();
        break;
    }
    state.loadedTabs.add(tab);
  }

  // ═══════════════════════════════════════
  // AUTO-REFRESH
  // ═══════════════════════════════════════
  function startRefresh() {
    stopRefresh();
    state.refreshInterval = setInterval(() => {
      const tab = $(".tab.active")?.dataset.tab || "overview";
      if (tab !== "events") loadTabData(tab);
      updateHealth();
      updateTimestamp();
    }, 30000);
  }
  function stopRefresh() {
    if (state.refreshInterval) {
      clearInterval(state.refreshInterval);
      state.refreshInterval = null;
    }
  }
  function updateTimestamp() {
    const el = $("#last-updated");
    if (el) el.textContent = "Updated: " + new Date().toLocaleTimeString();
  }

  // ═══════════════════════════════════════
  // THEME
  // ═══════════════════════════════════════
  function initTheme() {
    const saved = localStorage.getItem("orc-dash-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
    $("#theme-toggle").addEventListener("click", () => {
      const next =
        document.documentElement.getAttribute("data-theme") === "dark"
          ? "light"
          : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("orc-dash-theme", next);
      const tab = $(".tab.active")?.dataset.tab || "overview";
      state.loadedTabs.delete(tab);
      loadTabData(tab);
    });
  }

  // ═══════════════════════════════════════
  // CONTROLS
  // ═══════════════════════════════════════
  function initControls() {
    $("#period-select").addEventListener("change", (e) => {
      state.period = +e.target.value;
      state.loadedTabs.clear();
      loadTabData($(".tab.active")?.dataset.tab || "overview");
    });
    $("#refresh-btn").addEventListener("click", () => {
      const tab = $(".tab.active")?.dataset.tab || "overview";
      state.loadedTabs.delete(tab);
      loadTabData(tab);
      updateHealth();
      updateTimestamp();
    });
    $("#auto-refresh-toggle").addEventListener("change", (e) => {
      state.autoRefresh = e.target.checked;
      state.autoRefresh ? startRefresh() : stopRefresh();
    });

    // Models filters
    let modelsSearchTimeout;
    $("#models-search")?.addEventListener("input", () => {
      clearTimeout(modelsSearchTimeout);
      modelsSearchTimeout = setTimeout(() => {
        if (state.modelsData) {
          const models = safeArray(state.modelsData.data)
            .map(normalizeModel)
            .filter(Boolean);
          renderModelsTable(models);
        }
      }, 200);
    });
    $("#models-filter-status")?.addEventListener("change", () => {
      if (state.modelsData) {
        const models = safeArray(state.modelsData.data)
          .map(normalizeModel)
          .filter(Boolean);
        renderModelsTable(models);
      }
    });

    // Sessions
    let sessionSearchTimeout;
    $("#session-search")?.addEventListener("input", () => {
      clearTimeout(sessionSearchTimeout);
      sessionSearchTimeout = setTimeout(loadSessions, 300);
    });
    $("#modal-close")?.addEventListener("click", () => {
      $("#session-modal").style.display = "none";
    });
    $("#session-modal")?.addEventListener("click", (e) => {
      if (e.target === e.currentTarget) e.currentTarget.style.display = "none";
    });

    // Graph trace selector
    $("#graph-trace-select")?.addEventListener("change", async (e) => {
      const runId = e.target.value;
      if (runId) await loadGraphWaterfall(runId);
    });
  }

  // ═══════════════════════════════════════
  // GRAPH TRACING
  // ═══════════════════════════════════════

  const NODE_TYPE_COLORS = {
    classify: "#6366f1",
    route: "#8b5cf6",
    llm_fallback: "#a855f7",
    context: "#06b6d4",
    agent: "#10b981",
    collect: "#64748b",
    critic: "#f59e0b",
    synthesize: "#3b82f6",
    learn: "#71717a",
    direct: "#22c55e",
    other: "#94a3b8",
  };

  async function loadGraphTracing() {
    const days = state.period;
    const [overviewRes, tracesRes, statsRes, timelineRes] = await Promise.all([
      apiFetch("/graph/overview", { days }),
      apiFetch("/graph/traces", { days, limit: 50 }),
      apiFetch("/graph/stats", { days }),
      apiFetch("/graph/timeline", { days, resolution: "hour" }),
    ]);

    // KPIs
    if (overviewRes && !overviewRes._error && !overviewRes.error) {
      renderKPIGrid("graph-kpis", [
        {
          label: "Total Runs",
          value: formatTokens(overviewRes.total_runs || 0),
        },
        {
          label: "Avg Duration (ms)",
          value: formatLatency(overviewRes.avg_duration_ms || 0),
        },
        {
          label: "P95 Duration (ms)",
          value: formatLatency(overviewRes.p95_duration_ms || 0),
        },
        {
          label: "Error Rate",
          value: formatPercent((overviewRes.error_rate || 0) * 100),
        },
        {
          label: "Fallback Rate",
          value: formatPercent((overviewRes.fallback_rate || 0) * 100),
        },
        {
          label: "Avg Nodes/Run",
          value: safeNumber(overviewRes.avg_node_count, 0).toFixed(1),
        },
      ]);
    }

    // Timeline chart
    if (
      timelineRes &&
      !timelineRes._error &&
      timelineRes.data &&
      timelineRes.data.length
    ) {
      renderGraphTimeline(timelineRes.data);
    }

    // Traces table + populate selector
    if (tracesRes && !tracesRes._error && tracesRes.traces) {
      renderGraphTraces(tracesRes.traces);
      populateTraceSelector(tracesRes.traces);
    }

    // Node stats table + latency chart
    if (statsRes && !statsRes._error && statsRes.nodes) {
      renderGraphNodeStats(statsRes.nodes);
      renderGraphNodeLatencyChart(statsRes.nodes);
    }
  }

  function renderGraphTimeline(data) {
    const ctx = $("#graph-timeline-chart");
    if (!ctx) return;
    destroyChart("graph-timeline-chart");
    state.charts["graph-timeline-chart"] = new Chart(ctx, {
      type: "line",
      data: {
        labels: data.map((d) => d.period),
        datasets: [
          {
            label: "Runs",
            data: data.map((d) => d.runs),
            borderColor: "#6366f1",
            backgroundColor: "rgba(99,102,241,0.1)",
            fill: true,
            tension: 0.3,
            yAxisID: "y",
          },
          {
            label: "Avg Duration (ms)",
            data: data.map((d) => d.avg_duration_ms),
            borderColor: "#f59e0b",
            borderDash: [5, 5],
            fill: false,
            tension: 0.3,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { position: "left", title: { display: true, text: "Runs" } },
          y1: {
            position: "right",
            title: { display: true, text: "ms" },
            grid: { drawOnChartArea: false },
          },
        },
      },
    });
  }

  function renderGraphTraces(traces) {
    const tbody = $("#graph-traces-table tbody");
    if (!tbody) return;
    tbody.innerHTML = traces
      .map(
        (t) => `
      <tr>
        <td>${formatDate(t.timestamp)}</td>
        <td class="mono" title="${t.graph_run_id}">${t.graph_run_id.slice(0, 8)}</td>
        <td>${formatLatency(t.total_duration_ms)}</td>
        <td>${safeNumber(t.node_count)}</td>
        <td>${safeString(t.intent)}</td>
        <td>${safeString(t.model_used)}</td>
        <td>${(t.agents_invoked || []).join(", ") || "—"}</td>
        <td>${t.success ? '<span class="badge badge-ok">OK</span>' : '<span class="badge badge-error">ERR</span>'}</td>
      </tr>`,
      )
      .join("");
  }

  function populateTraceSelector(traces) {
    const sel = $("#graph-trace-select");
    if (!sel) return;
    sel.innerHTML =
      '<option value="">Select a trace...</option>' +
      traces
        .slice(0, 30)
        .map(
          (t) =>
            `<option value="${t.graph_run_id}">${formatDate(t.timestamp)} — ${safeString(t.intent, "?")}/${safeString(t.complexity, "?")} (${formatLatency(t.total_duration_ms)})</option>`,
        )
        .join("");
  }

  function renderGraphNodeStats(nodes) {
    const tbody = $("#graph-node-stats-table tbody");
    if (!tbody) return;
    tbody.innerHTML = nodes
      .map(
        (n) => `
      <tr>
        <td><span class="node-badge" style="background:${NODE_TYPE_COLORS[n.node_type] || NODE_TYPE_COLORS.other};color:#fff;padding:2px 6px;border-radius:4px;font-size:0.7rem">${n.node_name}</span></td>
        <td>${safeString(n.node_type)}</td>
        <td>${safeNumber(n.executions)}</td>
        <td>${safeNumber(n.avg_ms).toFixed(1)}</td>
        <td>${safeNumber(n.p50_ms).toFixed(1)}</td>
        <td>${safeNumber(n.p95_ms).toFixed(1)}</td>
        <td>${safeNumber(n.p99_ms).toFixed(1)}</td>
        <td>${safeNumber(n.max_ms).toFixed(1)}</td>
        <td>${safeNumber(n.errors)}</td>
        <td>${formatPercent(safeNumber(n.error_rate) * 100)}</td>
      </tr>`,
      )
      .join("");
  }

  function renderGraphNodeLatencyChart(nodes) {
    const ctx = $("#graph-node-latency-chart");
    if (!ctx || !nodes.length) return;
    destroyChart("graph-node-latency-chart");
    const sorted = [...nodes].sort((a, b) => b.p95_ms - a.p95_ms);
    state.charts["graph-node-latency-chart"] = new Chart(ctx, {
      type: "bar",
      data: {
        labels: sorted.map((n) => n.node_name),
        datasets: [
          {
            label: "P50",
            data: sorted.map((n) => n.p50_ms),
            backgroundColor: "rgba(99,102,241,0.7)",
          },
          {
            label: "P95",
            data: sorted.map((n) => n.p95_ms),
            backgroundColor: "rgba(245,158,11,0.7)",
          },
          {
            label: "P99",
            data: sorted.map((n) => n.p99_ms),
            backgroundColor: "rgba(239,68,68,0.7)",
          },
        ],
      },
      options: {
        responsive: true,
        indexAxis: "y",
        scales: {
          x: { title: { display: true, text: "Latency (ms)" } },
        },
      },
    });
  }

  async function loadGraphWaterfall(runId) {
    const container = $("#graph-waterfall");
    if (!container) return;
    container.innerHTML = '<p class="text-muted">Loading...</p>';

    const res = await apiFetch(`/graph/trace/${runId}`);
    if (!res || res._error || res.error || !res.nodes || !res.nodes.length) {
      container.innerHTML =
        '<p class="text-muted">No node data for this trace.</p>';
      return;
    }

    const nodes = res.nodes;
    // Calculate time base from first node timestamp
    const baseTime = new Date(nodes[0].timestamp).getTime();
    let maxEnd = 0;
    const entries = nodes.map((n) => {
      const startOffset = new Date(n.timestamp).getTime() - baseTime;
      const end = startOffset + n.duration_ms;
      if (end > maxEnd) maxEnd = end;
      return { ...n, startOffset, end };
    });

    const totalSpan = maxEnd || 1;

    // Build legend
    const usedTypes = [...new Set(entries.map((e) => e.node_type || "other"))];
    let html = '<div class="waterfall-legend">';
    for (const t of usedTypes) {
      html += `<div class="waterfall-legend-item"><div class="waterfall-legend-swatch" style="background:${NODE_TYPE_COLORS[t] || NODE_TYPE_COLORS.other}"></div>${t}</div>`;
    }
    html += "</div>";

    // Time scale
    const ticks = 5;
    html += '<div class="waterfall-scale">';
    for (let i = 0; i <= ticks; i++) {
      const tickPct = (i / ticks) * 100;
      const ms = ((i / ticks) * totalSpan).toFixed(0);
      html += `<span class="waterfall-scale-tick" style="left:${tickPct}%">${ms}ms</span>`;
    }
    html += "</div>";

    // Bars
    for (const entry of entries) {
      const leftPct = ((entry.startOffset / totalSpan) * 100).toFixed(2);
      const widthPct = Math.max(
        0.5,
        (entry.duration_ms / totalSpan) * 100,
      ).toFixed(2);
      const color = NODE_TYPE_COLORS[entry.node_type] || NODE_TYPE_COLORS.other;
      const errorClass = entry.success ? "" : " error";
      html += `
        <div class="waterfall-row">
          <div class="waterfall-label" title="${entry.node_name}">${entry.node_name}</div>
          <div class="waterfall-track">
            <div class="waterfall-bar${errorClass}" data-type="${entry.node_type || "other"}"
                 style="left:${leftPct}%;width:${widthPct}%;background:${entry.success ? color : "#ef4444"}"
                 title="${entry.node_name}: ${entry.duration_ms.toFixed(1)}ms${entry.success ? "" : " [ERROR: " + entry.error_type + "]"}">
              <span>${entry.node_name}</span>
              <span class="bar-duration">${entry.duration_ms.toFixed(1)}ms</span>
            </div>
          </div>
        </div>`;
    }

    container.innerHTML = html;
  }

  // ═══════════════════════════════════════
  // GEMILYNI TAB
  // ═══════════════════════════════════════
  async function loadGemilyni() {
    try {
      const [summary, runs, policy, errors] = await Promise.all([
        apiFetch("/gemilyni/summary?hours=24"),
        apiFetch("/gemilyni/runs?hours=24&limit=50"),
        apiFetch("/gemilyni/policy?hours=24"),
        apiFetch("/gemilyni/errors?hours=24"),
      ]);

      // Overview KPIs
      setText("gemilyni-total-runs", summary.total_runs || 0);
      setText("gemilyni-external-runs", summary.external_runs || 0);
      setText("gemilyni-local-runs", summary.local_runs || 0);
      setText(
        "gemilyni-success-rate",
        ((summary.success_rate || 0) * 100).toFixed(1) + "%",
      );
      setText(
        "gemilyni-failure-rate",
        ((summary.failure_rate || 0) * 100).toFixed(1) + "%",
      );
      setText("gemilyni-fallbacks", summary.fallbacks || 0);
      setText("gemilyni-policy-blocks", summary.policy_blocks || 0);
      setText("gemilyni-containers", summary.containers_created || 0);
      setText("gemilyni-workers", summary.workers_executed || 0);
      setText(
        "gemilyni-avg-duration",
        (summary.avg_duration_ms || 0).toFixed(0) + "ms",
      );
      setText(
        "gemilyni-avg-gemini",
        (summary.avg_gemini_ms || 0).toFixed(0) + "ms",
      );

      // Security KPIs
      setText("gemilyni-sensitive-blocked", summary.sensitive_blocked || 0);
      setText("gemilyni-files-blocked", summary.files_blocked || 0);
      setText("gemilyni-sources-blocked", summary.sources_blocked || 0);
      setText("gemilyni-violations", summary.violations || 0);
      setText("gemilyni-traversal-attempts", summary.traversal_attempts || 0);

      // Runs table
      const runsTbody = document.getElementById("gemilyni-runs-tbody");
      if (runsTbody && runs.runs) {
        runsTbody.innerHTML = runs.runs
          .map(
            (r) => `<tr>
          <td title="${r.run_id}">${(r.run_id || "").slice(0, 12)}</td>
          <td><span class="badge badge-${r.final_status === "success" ? "success" : "error"}">${r.final_status || "—"}</span></td>
          <td>${r.selected_path || "—"}</td>
          <td>${r.complexity || "—"}</td>
          <td>${r.intent || "—"}</td>
          <td>${r.workers_total || 0}</td>
          <td>${(r.total_duration_ms || 0).toFixed(0)}ms</td>
          <td>${r.fallback_used ? "Yes" : "No"}</td>
        </tr>`,
          )
          .join("");
      }

      // Policy violations table
      const policyTbody = document.getElementById("gemilyni-policy-tbody");
      if (policyTbody && policy.violations) {
        policyTbody.innerHTML = policy.violations
          .map(
            (v) => `<tr>
          <td title="${v.run_id}">${(v.run_id || "").slice(0, 12)}</td>
          <td>${v.policy_name || "—"}</td>
          <td>${v.violation_type || "—"}</td>
          <td>${v.blocked_item_ref || "—"}</td>
          <td>${v.reason || "—"}</td>
          <td><span class="badge badge-${v.severity === "error" ? "error" : "warning"}">${v.severity || "—"}</span></td>
        </tr>`,
          )
          .join("");
      }

      // Errors table
      const errorsTbody = document.getElementById("gemilyni-errors-tbody");
      if (errorsTbody && errors.errors) {
        errorsTbody.innerHTML = errors.errors
          .map(
            (e) => `<tr>
          <td title="${e.run_id}">${(e.run_id || "").slice(0, 12)}</td>
          <td>${e.phase || "—"}</td>
          <td>${e.error_type || "—"}</td>
          <td>${(e.error_message_redacted || "—").slice(0, 100)}</td>
          <td>${e.recoverable ? "Yes" : "No"}</td>
          <td>${e.fallback_used ? "Yes" : "No"}</td>
        </tr>`,
          )
          .join("");
      }
    } catch (err) {
      console.warn("Gemilyni tab load error:", err);
    }
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  // ═══════════════════════════════════════
  // BOOT
  // ═══════════════════════════════════════
  async function init() {
    initTheme();
    const tab = initTabs();
    initControls();
    initSSE();
    await Promise.all([loadTabData(tab), updateHealth()]);
    updateTimestamp();
    startRefresh();
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();
})();
