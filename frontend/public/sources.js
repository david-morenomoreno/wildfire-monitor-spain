let apiBaseUrl = "http://localhost:8000";
let lastSources = [];
let lastHealthByKey = new Map();
const HEALTH_DAYS = 14;

async function loadConfig() {
  const res = await fetch("/config");
  const data = await res.json();
  apiBaseUrl = data.apiBaseUrl;
}

function formatRelativeTime(isoString) {
  if (!isoString) return "never";
  const ms = Date.now() - new Date(isoString).getTime();
  const hours = ms / 3600000;
  if (hours < 1) return `${Math.max(1, Math.round(ms / 60000))}m ago`;
  if (hours < 48) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

// Maps the free-text status strings /api/sources actually returns ("active",
// "needs setup", "not yet polled", "reference only") onto the same
// ok/degraded/disrupted/skipped semantics status.html's health grid uses, so
// the two pages agree on what a color means.
function statusSemantics(status) {
  const normalized = (status || "").toLowerCase();
  if (normalized === "active") return "ok";
  if (normalized === "needs setup" || normalized === "degraded") return "degraded";
  if (normalized === "disrupted") return "disrupted";
  return "skipped"; // reference only, not yet polled, or anything unrecognized
}

function formatDayLabel(dateStr) {
  const date = new Date(`${dateStr}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (date.getTime() === today.getTime()) return "Today";
  return date.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}

// Same 14-day date range regardless of what a given source's own health rows
// cover, so every card's strip lines up on the same day columns.
function dayLabels() {
  return Array.from({ length: HEALTH_DAYS }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - (HEALTH_DAYS - 1 - i));
    return d.toISOString().slice(0, 10);
  });
}

function healthStripHtml(key) {
  const entry = lastHealthByKey.get(key);
  const days = dayLabels();
  const dotsHtml = days
    .map((date) => {
      const day = entry ? entry.days.find((d) => d.date === date) : null;
      const status = day ? day.status : null;
      const statusClass = status ? `status-${status}` : "status-none";
      const title = status
        ? `${date} — ${status}${day.message ? ": " + day.message : ""}`
        : `${date} — no check ran`;
      return `<span class="health-dot ${statusClass}" title="${title.replace(/"/g, "&quot;")}"></span>`;
    })
    .join("");
  return (
    `<div class="health-strip">` +
    `<span class="health-strip-label">${formatDayLabel(days[0])}</span>` +
    `<div class="health-strip-dots">${dotsHtml}</div>` +
    `<span class="health-strip-label">Today</span>` +
    `</div>`
  );
}

const CATEGORY_LABELS = {
  satellite: "Satellite",
  administration: "Administration bulletins",
  "regional-incidents": "Regional live incidents",
  telegram: "Telegram",
  webcams: "Webcams",
  reference: "Reference",
};
const CATEGORY_ORDER = [
  "satellite",
  "administration",
  "regional-incidents",
  "telegram",
  "webcams",
  "reference",
];

function sourceCardHtml(source) {
  const semantics = statusSemantics(source.status);
  const refreshBtn = source.refresh_url
    ? `<div class="source-card-actions"><button class="refresh-btn" data-key="${source.key}" data-refresh-url="${source.refresh_url}">Refresh now</button></div>`
    : "";
  return (
    `<div class="source-card" data-name="${source.name.toLowerCase()}">` +
    `<span class="source-status-dot ${semantics}"></span>` +
    `<div class="source-card-body">` +
    `<div class="source-card-top">` +
    `<a class="source-card-name" href="${source.url}" target="_blank" rel="noopener">${source.name}</a>` +
    `<span class="status-badge ${semantics}">${source.status}</span>` +
    `</div>` +
    (source.detail ? `<div class="source-card-detail">${source.detail}</div>` : "") +
    `<div class="source-card-meta">Last success: ${formatRelativeTime(source.last_success_at)}</div>` +
    healthStripHtml(source.key) +
    refreshBtn +
    `</div>` +
    `</div>`
  );
}

function categorySectionHtml(category, sources) {
  const label = CATEGORY_LABELS[category] || category;
  return (
    `<div class="category-section" data-category="${category}">` +
    `<h2 class="category-title">${label}<span class="category-count">(${sources.length})</span></h2>` +
    `<div class="category-cards">${sources.map(sourceCardHtml).join("")}</div>` +
    `</div>`
  );
}

function renderSources(sources) {
  const content = document.getElementById("content");
  if (sources.length === 0) {
    content.innerHTML = `<div class="empty">No sources match your search.</div>`;
    return;
  }
  const byCategory = new Map();
  sources.forEach((source) => {
    if (!byCategory.has(source.category)) byCategory.set(source.category, []);
    byCategory.get(source.category).push(source);
  });
  const orderedCategories = [
    ...CATEGORY_ORDER.filter((c) => byCategory.has(c)),
    ...Array.from(byCategory.keys()).filter((c) => !CATEGORY_ORDER.includes(c)),
  ];
  content.innerHTML = orderedCategories
    .map((category) => categorySectionHtml(category, byCategory.get(category)))
    .join("");

  content.querySelectorAll(".refresh-btn").forEach((btn) => {
    btn.addEventListener("click", () => refreshSource(btn.dataset.key, btn.dataset.refreshUrl, btn));
  });
}

// Same refresh pattern as status.js's refreshSource - POST the source's
// refresh_url, then re-fetch /api/sources to reflect the new status/detail.
async function refreshSource(key, refreshUrl, button) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    const res = await fetch(`${apiBaseUrl}${refreshUrl}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    await loadSources();
    applySearchFilter();
  } catch (err) {
    button.textContent = "Failed";
    button.title = err.message;
    setTimeout(() => {
      button.textContent = originalText;
      button.disabled = false;
    }, 2500);
  }
}

function applySearchFilter() {
  const query = document.getElementById("source-search").value.trim().toLowerCase();
  const filtered = query ? lastSources.filter((s) => s.name.toLowerCase().includes(query)) : lastSources;
  renderSources(filtered);
}

async function loadSources() {
  const content = document.getElementById("content");
  try {
    const [sourcesRes, healthRes] = await Promise.all([
      fetch(`${apiBaseUrl}/api/sources`),
      fetch(`${apiBaseUrl}/api/health?days=${HEALTH_DAYS}`),
    ]);
    lastSources = await sourcesRes.json();
    const health = await healthRes.json();
    lastHealthByKey = new Map(health.map((h) => [h.source_key, h]));
  } catch (err) {
    content.innerHTML = `<div class="empty">Failed to load sources: ${err.message}</div>`;
    throw err;
  }
}

document.getElementById("source-search").addEventListener("input", applySearchFilter);

(function initThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  const applyIcon = (theme) => {
    btn.textContent = theme === "light" ? "☀️" : "🌙";
    btn.title = theme === "light" ? "Cambiar a modo oscuro" : "Cambiar a modo claro";
  };
  applyIcon(document.documentElement.dataset.theme || "dark");
  btn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("wm-theme", next);
    applyIcon(next);
  });
})();

(async function init() {
  await loadConfig();
  await loadSources();
  applySearchFilter();
})();
