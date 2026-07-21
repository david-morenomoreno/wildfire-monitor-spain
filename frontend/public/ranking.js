// Ranking & per-incident report page. Deliberately self-contained (like
// sources.js) rather than sharing app.js's scope - app.js assumes a Leaflet
// #map element and does a lot of map-specific init on load that has nothing
// to do with this page.

let apiBaseUrl = "http://localhost:8000";
let lastRanked = [];
// Populated only while merge mode is active - see loadMergeCandidates(). Uses
// GET /api/incidents (undeduped) rather than /api/incidents/rankings,
// because the rankings endpoint collapses probable-duplicate rows down to
// one "richest" row per (locality, province, first-detection day) for
// display (_dedupe_by_place in routers/incidents.py) - exactly the rows an
// admin needs to SEE and select in order to merge them away for good.
let mergeCandidates = [];

async function loadConfig() {
  const res = await fetch("/config");
  const data = await res.json();
  apiBaseUrl = data.apiBaseUrl;
}

// ---------- Shared label/badge helpers (kept consistent with app.js) ----------
const RISK_LABELS = { low: "Bajo", moderate: "Moderado", high: "Alto", critical: "Crítico" };
const STATUS_LABELS = { active: "Activo", cooling: "En enfriamiento", archived: "Archivado" };
const SORT_METRIC_LABEL = {
  severity: "Gravedad",
  area: "Ha",
  detections: "Detecc.",
  duration: "Duración",
};

const ICON_SVG_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  shield: `<svg ${ICON_SVG_ATTRS} width="12" height="12"><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>`,
  satellite: `<svg ${ICON_SVG_ATTRS} width="12" height="12"><path d="M13 7l4 4-6 6-4-4a5.66 5.66 0 0 1 6-6z"/><path d="M3 21l3.5-3.5"/><path d="M17 3a11 11 0 0 1 4 4M14 6a7 7 0 0 1 4 4"/></svg>`,
  send: `<svg ${ICON_SVG_ATTRS} width="12" height="12"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>`,
  camera: `<svg ${ICON_SVG_ATTRS} width="13" height="13"><path d="M4 8h3l1.5-2h7L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.2"/></svg>`,
  clock: `<svg ${ICON_SVG_ATTRS} width="11" height="11"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  flame: `<svg ${ICON_SVG_ATTRS} width="12" height="12"><path d="M12 2c1 3-3 4-3 8a3 3 0 0 0 6 0c0-1-.5-2-1-2.5.5 2 .5 4-1 5.5a4 4 0 0 1-4-4c0-3 2-4 2-7-2 1-4 4-4 7a5 5 0 0 0 10 0c0-5-3-6-5-7z"/></svg>`,
};

const EVENT_TYPE_ICON = {
  detection: ICONS.flame,
  status_change: ICONS.clock,
  telegram_message: ICONS.send,
  satellite_imagery: ICONS.camera,
  regional_status: ICONS.shield,
};

function riskBadgeHtml(riskLevel, status) {
  const inactiveClass = status && status !== "active" ? " risk-badge-inactive" : "";
  return `<span class="risk-badge risk-${riskLevel}${inactiveClass}">${RISK_LABELS[riskLevel] || riskLevel}</span>`;
}

function durationLabel(hours) {
  if (hours < 1) return "<1h";
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

function formatDateTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("es-ES", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

// The "area" sort itself only ranks EFFIS-confirmed incidents today (the
// backend filters to area_ha != null for that sort - see
// get_incident_rankings), so this fallback is currently defensive/future-
// proofing rather than reachable - but the report view (reportHtml below)
// DOES hit it constantly, since most incidents opened from a non-area sort
// have no official area_ha at all.
function metricValueFor(sort, incident) {
  if (sort === "area") {
    if (incident.area_ha != null) return Math.round(incident.area_ha).toLocaleString("es-ES");
    if (incident.area_ha_estimated != null) return `${Math.round(incident.area_ha_estimated).toLocaleString("es-ES")} (est.)`;
    return "—";
  }
  if (sort === "detections") return incident.detection_count.toLocaleString("es-ES");
  if (sort === "duration") return durationLabel(incident.duration_hours);
  return Math.round(incident.severity_score).toLocaleString("es-ES");
}

// ---------- Ranking view ----------
function sourceChipHtml(present, icon, title) {
  return `<span class="source-chip${present ? " present" : ""}" title="${title}">${icon}</span>`;
}

// Incident display name: a manually-set official_name always wins over the
// reverse-geocoded locality (see backend PATCH /api/incidents/{id}).
function displayName(incident) {
  return incident.official_name || incident.locality || `Foco sin nombre #${incident.id}`;
}

let mergeModeActive = false;
const selectedForMerge = new Set();

function rankingRowHtml(incident, sort) {
  const place = [incident.locality, incident.province].filter(Boolean).join(", ") || "Ubicación sin resolver";
  const checkbox = mergeModeActive
    ? `<input type="checkbox" class="row-select-checkbox" data-select-id="${incident.id}" ${selectedForMerge.has(incident.id) ? "checked" : ""} />`
    : "";
  const selectedClass = mergeModeActive && selectedForMerge.has(incident.id) ? " selected" : "";
  return (
    `<div class="ranking-row${mergeModeActive ? " merge-mode" : ""}${selectedClass}" data-id="${incident.id}">` +
    (mergeModeActive ? `<div>${checkbox}</div>` : "") +
    `<div class="ranking-rank">${incident.rank}</div>` +
    `<div class="ranking-name-block">` +
    `<div class="ranking-name">${displayName(incident)}</div>` +
    `<div class="ranking-place">${place}</div>` +
    `</div>` +
    `<div>${riskBadgeHtml(incident.risk_level, incident.status)}</div>` +
    `<div class="ranking-metric">` +
    `<div class="ranking-metric-value">${metricValueFor(sort, incident)}</div>` +
    `<div class="ranking-metric-label">${SORT_METRIC_LABEL[sort]}</div>` +
    `</div>` +
    `<div class="ranking-sources">` +
    sourceChipHtml(incident.has_regional_status, ICONS.shield, "Estado oficial regional") +
    sourceChipHtml(incident.has_satellite_imagery, ICONS.satellite, "Imágenes de satélite") +
    sourceChipHtml(incident.has_telegram_mentions, ICONS.send, "Menciones en Telegram") +
    `</div>` +
    `</div>`
  );
}

function updateMergeBar() {
  const bar = document.getElementById("merge-bar");
  const count = selectedForMerge.size;
  bar.classList.toggle("visible", mergeModeActive && count > 0);
  document.getElementById("merge-bar-count").innerHTML = `<b>${count}</b> incidente${count === 1 ? "" : "s"} seleccionado${count === 1 ? "" : "s"}`;
  document.getElementById("merge-bar-btn").disabled = count < 2;
}

function renderRanking(incidents, sort) {
  const content = document.getElementById("ranking-content");
  if (incidents.length === 0) {
    content.innerHTML = `<div class="empty">No hay incidentes que coincidan con este filtro.</div>`;
    return;
  }
  content.innerHTML = `<div class="ranking-table">${incidents.map((inc) => rankingRowHtml(inc, sort)).join("")}</div>`;
  content.querySelectorAll(".ranking-row").forEach((row) => {
    const id = Number(row.dataset.id);
    if (mergeModeActive) {
      row.addEventListener("click", (event) => {
        if (event.target.matches(".row-select-checkbox")) return;
        toggleSelection(id);
      });
      const checkboxEl = row.querySelector(".row-select-checkbox");
      if (checkboxEl) checkboxEl.addEventListener("change", () => toggleSelection(id));
    } else {
      row.addEventListener("click", () => {
        window.location.hash = `#/incident/${id}`;
      });
    }
  });
  updateMergeBar();
}

function toggleSelection(id) {
  if (selectedForMerge.has(id)) selectedForMerge.delete(id);
  else selectedForMerge.add(id);
  renderRanking(mergeCandidates, "detections");
}

// Fetches the full, undeduped incident list (see mergeCandidates comment
// above) and adds the rank/duration_hours fields rankingRowHtml/metricValueFor
// expect (rankings-only fields on RankedIncidentOut, absent from plain
// FireIncidentOut), sorted by detection count so likely-duplicate rows
// (identical/near-identical counts) sit next to each other.
async function loadMergeCandidates() {
  const content = document.getElementById("ranking-content");
  content.innerHTML = `<div class="empty">Cargando incidentes (incluyendo posibles duplicados)…</div>`;
  const res = await fetch(`${apiBaseUrl}/api/incidents?hours=${24 * 30}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const incidents = await res.json();
  incidents.sort((a, b) => b.detection_count - a.detection_count);
  mergeCandidates = incidents.map((inc, index) => ({
    ...inc,
    rank: index + 1,
    duration_hours: (new Date(inc.last_detected_at) - new Date(inc.first_detected_at)) / 3600000,
  }));
}

function updateScopeNote(days, count) {
  const note = document.getElementById("scope-note");
  const scope = days ? `los últimos ${days} días` : "todo el histórico registrado por este monitor";
  note.textContent = `Mostrando ${count} incendio${count === 1 ? "" : "s"} de ${scope}.`;
}

async function loadRanking() {
  const content = document.getElementById("ranking-content");
  const sort = document.getElementById("sort-select").value;
  const days = document.getElementById("days-select").value;
  content.innerHTML = `<div class="empty">Cargando…</div>`;
  try {
    const params = new URLSearchParams({ sort, limit: "50" });
    if (days) params.set("days", days);
    const res = await fetch(`${apiBaseUrl}/api/incidents/rankings?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    lastRanked = await res.json();
    renderRanking(lastRanked, sort);
    updateScopeNote(days, lastRanked.length);
  } catch (err) {
    content.innerHTML = `<div class="empty">No se pudo cargar el ranking: ${err.message}</div>`;
  }
}

// ---------- Report (detail) view ----------
function personnelGridHtml(personnelSummaryJson) {
  if (!personnelSummaryJson) return "";
  let summary;
  try {
    summary = JSON.parse(personnelSummaryJson);
  } catch {
    return "";
  }
  const total = summary.total_actuando || 0;
  const entries = Object.entries(summary).filter(([key, count]) => key !== "total_actuando" && count);
  if (entries.length === 0 && !total) return "";
  const items = entries
    .map(
      ([category, count]) =>
        `<div class="personnel-item"><div class="personnel-item-value">${count}</div><div class="personnel-item-label">${category}</div></div>`
    )
    .join("");
  return (
    (total ? `<div class="personnel-total">${total} medio${total === 1 ? "" : "s"} desplegado${total === 1 ? "" : "s"} en total</div>` : "") +
    (items ? `<div class="personnel-grid">${items}</div>` : "")
  );
}

function regionalStatusSectionHtml(records) {
  if (!records || records.length === 0) {
    return `<div class="report-section-empty">Sin datos de estado oficial regional para este incidente.</div>`;
  }
  const cards = records
    .map((record) => {
      const place = [record.municipality, record.province].filter(Boolean).join(" · ");
      const dates = [
        record.started_at ? `Inicio: ${formatDateTime(record.started_at)}` : null,
        record.controlled_at ? `Controlado: ${formatDateTime(record.controlled_at)}` : null,
        record.extinguished_at ? `Extinguido: ${formatDateTime(record.extinguished_at)}` : null,
      ]
        .filter(Boolean)
        .join(" &nbsp;·&nbsp; ");
      return (
        `<div class="regional-status-card">` +
        `<div class="regional-status-top">` +
        `<span class="regional-status-pill">${record.status}</span>` +
        (place ? `<span class="regional-status-place">${place}</span>` : "") +
        (record.cause ? `<span class="regional-status-place">Causa: ${record.cause}</span>` : "") +
        `</div>` +
        personnelGridHtml(record.personnel_summary) +
        (dates ? `<div class="regional-dates">${dates}</div>` : "") +
        `</div>`
      );
    })
    .join("");
  return cards;
}

function detectionSourceBreakdownHtml(sources) {
  if (!sources || sources.length === 0) {
    return `<div class="report-section-empty">Sin detecciones satelitales asociadas (posible fuente puramente administrativa/comunitaria).</div>`;
  }
  const max = Math.max(...sources.map((s) => s.count));
  const rows = sources
    .map(
      (s) =>
        `<div class="source-breakdown-row">` +
        `<span class="source-breakdown-label">${s.source}</span>` +
        `<div class="source-breakdown-bar-wrap"><div class="source-breakdown-bar" style="width:${Math.max(4, (s.count / max) * 100)}%"></div></div>` +
        `<span class="source-breakdown-count">${s.count}</span>` +
        `</div>`
    )
    .join("");
  return (
    `<div class="source-breakdown">${rows}</div>` +
    `<div class="report-section-empty" style="margin-top:8px; font-style:normal;">Aproximación por proximidad geográfica y ventana temporal - no es un recuento almacenado por incidente.</div>`
  );
}

function satelliteCarouselHtml(scenes) {
  if (!scenes || scenes.length === 0) {
    return `<div class="report-section-empty">Sin escenas de satélite (Copernicus) descubiertas para este incidente.</div>`;
  }
  const slides = scenes
    .map((scene) => {
      const url = `${apiBaseUrl}/api/copernicus/scenes/${scene.id}/thumbnail`;
      const dateLabel = new Date(scene.captured_at).toLocaleDateString("es-ES", { day: "numeric", month: "short" });
      const cloud = scene.cloud_cover != null ? ` · ${scene.cloud_cover.toFixed(0)}% nubes` : "";
      return (
        `<div class="satellite-slide">` +
        `<img src="${url}" loading="lazy" data-lightbox="${url}" />` +
        `<div class="satellite-slide-caption">${dateLabel}${cloud}</div>` +
        `</div>`
      );
    })
    .join("");
  return `<div class="satellite-carousel">${slides}</div>`;
}

function telegramSectionHtml(messages) {
  if (!messages || messages.length === 0) {
    return `<div class="report-section-empty">Sin menciones en los canales de Telegram monitorizados.</div>`;
  }
  return messages
    .slice(0, 20)
    .map((msg) => {
      const thumb = msg.media_path
        ? `<img src="${apiBaseUrl}/media/${encodeURIComponent(msg.media_path)}" class="telegram-thumb" data-lightbox="${apiBaseUrl}/media/${encodeURIComponent(msg.media_path)}" />`
        : "";
      return (
        `<div class="telegram-item">` +
        `<div class="telegram-item-time">${formatDateTime(msg.posted_at)}</div>` +
        (msg.text ? `<div class="telegram-item-text">${msg.text}</div>` : "") +
        thumb +
        `</div>`
      );
    })
    .join("");
}

function timelineEventImageUrl(event) {
  if (!event.raw_data) return null;
  try {
    const data = JSON.parse(event.raw_data);
    if (data.scene_db_id) return `${apiBaseUrl}/api/copernicus/scenes/${data.scene_db_id}/thumbnail`;
    if (data.media_path) return `${apiBaseUrl}/media/${encodeURIComponent(data.media_path)}`;
  } catch {
    return null;
  }
  return null;
}

function timelineItemHtml(event) {
  const imageUrl = event.event_type === "satellite_imagery" ? null : timelineEventImageUrl(event);
  const icon = EVENT_TYPE_ICON[event.event_type] || "";
  return (
    `<div class="timeline-item">` +
    `<span class="timeline-dot timeline-dot-${event.event_type || "default"}">${icon}</span>` +
    `<div class="timeline-time">${formatDateTime(event.occurred_at)}</div>` +
    `<div class="timeline-title">${event.title}</div>` +
    (event.description ? `<div class="timeline-desc">${event.description}</div>` : "") +
    (imageUrl ? `<img src="${imageUrl}" class="timeline-thumb" data-lightbox="${imageUrl}" />` : "") +
    `</div>`
  );
}

function timelineSectionHtml(events) {
  if (!events || events.length === 0) {
    return `<div class="report-section-empty">Sin eventos registrados.</div>`;
  }
  return `<div class="timeline-list">${events.map(timelineItemHtml).join("")}</div>`;
}

function reportHtml(report) {
  const incident = report.incident;
  const name = displayName(incident);
  const place = [incident.province, incident.country_code].filter(Boolean).join(" · ");
  const hasOfficialArea = incident.area_ha != null;
  // area_ha is only ever populated from an EFFIS burnt-area detection
  // (most incidents never get one) - area_ha_estimated is the backend's
  // concave-hull estimate over this incident's own detections (see
  // services/area_estimate.py), computed only when the official figure is
  // missing. Same "oficial" vs "estimado" honesty convention the map's own
  // sidebar uses (app.js) - never presented as if it were the EFFIS number.
  const hasEstimatedArea = !hasOfficialArea && incident.area_ha_estimated != null;

  return (
    `<div class="report-header">` +
    `<div class="report-title">${name}</div>` +
    (place ? `<div class="report-subtitle">${place}</div>` : "") +
    `<div class="rename-row">` +
    `<button class="rename-btn" id="rename-toggle-btn">✎ Editar nombre</button>` +
    `</div>` +
    `<div class="rename-edit" id="rename-edit">` +
    `<input type="text" id="rename-input" placeholder="Nombre oficial (p. ej. IF Los Gallardos)" value="${incident.official_name ? incident.official_name.replace(/"/g, "&quot;") : ""}" />` +
    `<button class="rename-btn" id="rename-save-btn">Guardar</button>` +
    `<button class="rename-btn" id="rename-cancel-btn">Cancelar</button>` +
    `</div>` +
    `<div class="report-badges">` +
    `${riskBadgeHtml(incident.risk_level, incident.status)}` +
    `<span class="risk-badge" style="background:var(--bg-elevated-hover); color:var(--text-secondary);">${STATUS_LABELS[incident.status] || incident.status}</span>` +
    `</div>` +
    `<div class="report-metrics">` +
    `<div class="report-metric"><div class="report-metric-value">${incident.detection_count.toLocaleString("es-ES")}</div><div class="report-metric-label">Detecciones</div></div>` +
    `<div class="report-metric"><div class="report-metric-value">${durationLabel(report.duration_hours)}</div><div class="report-metric-label">Tiempo activo</div></div>` +
    (hasOfficialArea
      ? `<div class="report-metric"><div class="report-metric-value">${Math.round(incident.area_ha).toLocaleString("es-ES")}</div><div class="report-metric-label">Hectáreas (oficial)</div></div>`
      : hasEstimatedArea
        ? `<div class="report-metric"><div class="report-metric-value">${Math.round(incident.area_ha_estimated).toLocaleString("es-ES")}</div><div class="report-metric-label">Hectáreas (estimado)</div></div>`
        : "") +
    `<div class="report-metric"><div class="report-metric-value">${formatDateTime(incident.first_detected_at)}</div><div class="report-metric-label">Primera detección</div></div>` +
    `<div class="report-metric"><div class="report-metric-value">${formatDateTime(incident.last_detected_at)}</div><div class="report-metric-label">Última detección</div></div>` +
    `</div>` +
    `</div>` +
    `<div class="report-section">` +
    `<div class="report-section-title">${ICONS.satellite} Detecciones por satélite</div>` +
    detectionSourceBreakdownHtml(report.detection_sources) +
    `</div>` +
    `<div class="report-section">` +
    `<div class="report-section-title">${ICONS.shield} Estado oficial y medios desplegados</div>` +
    regionalStatusSectionHtml(report.regional_status) +
    `</div>` +
    `<div class="report-section">` +
    `<div class="report-section-title">${ICONS.camera} Imágenes de satélite (Copernicus)</div>` +
    satelliteCarouselHtml(report.satellite_scenes) +
    `</div>` +
    `<div class="report-section">` +
    `<div class="report-section-title">${ICONS.send} Menciones en Telegram</div>` +
    telegramSectionHtml(report.telegram_messages) +
    `</div>` +
    `<div class="report-section">` +
    `<div class="report-section-title">${ICONS.clock} Cronología completa</div>` +
    timelineSectionHtml(report.timeline) +
    `</div>`
  );
}

async function loadReport(incidentId) {
  const content = document.getElementById("report-content");
  content.innerHTML = `<div class="empty">Cargando informe…</div>`;
  try {
    const res = await fetch(`${apiBaseUrl}/api/incidents/${incidentId}/report`);
    if (!res.ok) throw new Error(res.status === 404 ? "Incidente no encontrado" : `HTTP ${res.status}`);
    const report = await res.json();
    content.innerHTML = reportHtml(report);
    attachLightboxHandlers();
    attachRenameHandlers(incidentId);
  } catch (err) {
    content.innerHTML = `<div class="empty">No se pudo cargar el informe: ${err.message}</div>`;
  }
}

// ---------- Rename (manual name override) ----------
function attachRenameHandlers(incidentId) {
  const toggleBtn = document.getElementById("rename-toggle-btn");
  const editRow = document.getElementById("rename-edit");
  const input = document.getElementById("rename-input");
  const saveBtn = document.getElementById("rename-save-btn");
  const cancelBtn = document.getElementById("rename-cancel-btn");

  toggleBtn.addEventListener("click", () => {
    editRow.classList.add("active");
    input.focus();
  });
  cancelBtn.addEventListener("click", () => editRow.classList.remove("active"));
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "Guardando…";
    try {
      const res = await fetch(`${apiBaseUrl}/api/incidents/${incidentId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ official_name: input.value.trim() || null }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadReport(incidentId);
    } catch (err) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Guardar";
      alert(`No se pudo guardar el nombre: ${err.message}`);
    }
  });
}

function attachLightboxHandlers() {
  const backdrop = document.getElementById("lightbox");
  const img = document.getElementById("lightbox-img");
  document.querySelectorAll("[data-lightbox]").forEach((el) => {
    el.addEventListener("click", () => {
      img.src = el.dataset.lightbox;
      backdrop.classList.add("active");
    });
  });
}

document.getElementById("lightbox").addEventListener("click", () => {
  document.getElementById("lightbox").classList.remove("active");
});

// ---------- Hash-based routing between the ranking list and one incident's report ----------
function route() {
  const match = /^#\/incident\/(\d+)$/.exec(window.location.hash);
  const rankingView = document.getElementById("ranking-view");
  const reportView = document.getElementById("report-view");
  if (match) {
    rankingView.classList.add("hidden");
    reportView.classList.add("active");
    loadReport(match[1]);
    window.scrollTo(0, 0);
  } else {
    reportView.classList.remove("active");
    rankingView.classList.remove("hidden");
    if (lastRanked.length === 0) loadRanking();
  }
}

document.getElementById("report-back").addEventListener("click", () => {
  window.location.hash = "";
});
window.addEventListener("hashchange", route);
document.getElementById("sort-select").addEventListener("change", loadRanking);
document.getElementById("days-select").addEventListener("change", loadRanking);

// ---------- Merge mode ----------
document.getElementById("merge-mode-toggle").addEventListener("click", async () => {
  mergeModeActive = !mergeModeActive;
  selectedForMerge.clear();
  const toggleBtn = document.getElementById("merge-mode-toggle");
  toggleBtn.classList.toggle("active", mergeModeActive);
  toggleBtn.textContent = mergeModeActive ? "Cancelar selección" : "Fusionar incidentes";
  if (mergeModeActive) {
    try {
      await loadMergeCandidates();
      renderRanking(mergeCandidates, "detections");
    } catch (err) {
      document.getElementById("ranking-content").innerHTML = `<div class="empty">No se pudo cargar la lista para fusión: ${err.message}</div>`;
    }
  } else {
    renderRanking(lastRanked, document.getElementById("sort-select").value);
  }
});

document.getElementById("merge-bar-btn").addEventListener("click", () => openMergeModal());

function selectedIncidents() {
  const source = mergeModeActive ? mergeCandidates : lastRanked;
  return source.filter((inc) => selectedForMerge.has(inc.id));
}

function openMergeModal() {
  const incidents = selectedIncidents();
  if (incidents.length < 2) return;
  document.getElementById("merge-modal-count").textContent = incidents.length;
  document.getElementById("merge-modal-error").textContent = "";
  document.getElementById("merge-name-input").value = incidents.map((i) => i.official_name).find(Boolean) || "";

  const defaultSurvivor = incidents.reduce((a, b) => (b.detection_count > a.detection_count ? b : a));
  const list = document.getElementById("merge-survivor-list");
  list.innerHTML = incidents
    .map(
      (inc) =>
        `<label class="modal-survivor-option">` +
        `<input type="radio" name="survivor" value="${inc.id}" ${inc.id === defaultSurvivor.id ? "checked" : ""} />` +
        `#${inc.id} — ${displayName(inc)} (${inc.detection_count} detecciones, ${STATUS_LABELS[inc.status] || inc.status})` +
        `</label>`
    )
    .join("");

  document.getElementById("merge-modal").classList.add("active");
}

function closeMergeModal() {
  document.getElementById("merge-modal").classList.remove("active");
}

document.getElementById("merge-modal-cancel").addEventListener("click", closeMergeModal);
document.getElementById("merge-modal").addEventListener("click", (event) => {
  if (event.target.id === "merge-modal") closeMergeModal();
});

document.getElementById("merge-modal-confirm").addEventListener("click", async () => {
  const incidents = selectedIncidents();
  const survivorInput = document.querySelector('input[name="survivor"]:checked');
  const confirmBtn = document.getElementById("merge-modal-confirm");
  const errorEl = document.getElementById("merge-modal-error");
  if (!survivorInput) {
    errorEl.textContent = "Selecciona un incidente superviviente.";
    return;
  }
  const officialName = document.getElementById("merge-name-input").value.trim();
  confirmBtn.disabled = true;
  confirmBtn.textContent = "Fusionando…";
  errorEl.textContent = "";
  try {
    const res = await fetch(`${apiBaseUrl}/api/incidents/merge`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        incident_ids: incidents.map((i) => i.id),
        survivor_id: Number(survivorInput.value),
        official_name: officialName || null,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ? JSON.stringify(body.detail) : `HTTP ${res.status}`);
    }
    const merged = await res.json();
    closeMergeModal();
    mergeModeActive = false;
    selectedForMerge.clear();
    document.getElementById("merge-mode-toggle").classList.remove("active");
    document.getElementById("merge-mode-toggle").textContent = "Fusionar incidentes";
    await loadRanking();
    window.location.hash = `#/incident/${merged.id}`;
  } catch (err) {
    errorEl.textContent = `No se pudo fusionar: ${err.message}`;
  } finally {
    confirmBtn.disabled = false;
    confirmBtn.textContent = "Confirmar fusión";
  }
});

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
  await loadRanking();
  route();
})();
