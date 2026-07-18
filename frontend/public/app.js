const statusEl = document.getElementById("status");
let apiBaseUrl = "http://localhost:8000";

const map = L.map("map").setView([40.0, -3.7], 6); // centered on Spain

const osmLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
});
// Windy-style outdoor/topo basemap - free, no key, standard attribution required.
const topoLayer = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
  maxZoom: 17,
  attribution: '&copy; OpenStreetMap contributors, SRTM | &copy; <a href="https://opentopomap.org">OpenTopoMap</a>',
});
let currentBaseLayer = osmLayer;
currentBaseLayer.addTo(map);

const markersLayer = L.layerGroup().addTo(map);
const webcamsLayer = L.layerGroup();

// NASA GIBS true-color satellite imagery (no API key needed). "best" auto-picks
// the least cloudy available product for the requested date; GoogleMapsCompatible_Level9
// tops out at zoom 9, so tiles beyond that are upsampled rather than missing.
let satelliteLayer = null;

function buildSatelliteLayer(dateStr) {
  const url = `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/MODIS_Terra_CorrectedReflectance_TrueColor/default/${dateStr}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg`;
  return L.tileLayer(url, {
    maxZoom: 12,
    maxNativeZoom: 9,
    attribution: "NASA GIBS / MODIS Terra",
  });
}

function updateSatelliteLayer() {
  const toggle = document.getElementById("satellite-toggle");
  const dateInput = document.getElementById("satellite-date");
  if (satelliteLayer) {
    map.removeLayer(satelliteLayer);
    satelliteLayer = null;
  }
  if (toggle.checked && dateInput.value) {
    map.removeLayer(currentBaseLayer); // satellite imagery replaces the basemap, not sits under it
    satelliteLayer = buildSatelliteLayer(dateInput.value);
    satelliteLayer.addTo(map);
  } else {
    currentBaseLayer.addTo(map);
  }
}

// Switches the underlying street/topo basemap. If satellite imagery is
// currently showing, just swaps which layer is "waiting" for when satellite
// gets toggled off, rather than fighting it for the map's base slot now.
function setBasemapStyle(style) {
  const nextLayer = style === "topo" ? topoLayer : osmLayer;
  if (nextLayer === currentBaseLayer) return;
  const satelliteShowing = document.getElementById("satellite-toggle").checked && satelliteLayer;
  if (!satelliteShowing) map.removeLayer(currentBaseLayer);
  currentBaseLayer = nextLayer;
  if (!satelliteShowing) currentBaseLayer.addTo(map);
}

// Data source (FIRMS/EFFIS/satellite instrument) is intentionally not shown -
// styling only encodes recency (color). Every hotspot dot renders at the same
// fixed radius regardless of how many nearby detections it represents, so the
// ONLY thing that varies across dots is color - which lets a red-to-yellow
// gradient across a cluster read as "this is the direction the fire has been
// spreading" (red = recent edge, yellow = where it started). Encoding a
// second variable (size = detection count) on top of that would compete with
// and muddy the color signal.
const HOTSPOT_STROKE = "#333"; // outline on the map's light OSM basemap, not the dark panel/popups
const REPORT_COLOR = "#2dd4bf";
const HOTSPOT_DOT_RADIUS = 4; // fixed for every dot at every zoom - see note above

function setStatus(text) {
  statusEl.textContent = text;
}

// FIXED absolute age buckets (not relative to whatever date-range window
// happens to be selected - an 18h-old detection should look the same whether
// you're browsing "Last 24h" or "Last 7 days"). This now actually matches the
// <12h/<24h/<48h/<72h discrete-bucket convention other fire-monitoring maps
// use (e.g. Bseed WATCH/Pyrofire) that this comment already referenced before
// this change - the previous version only ever interpolated red-to-yellow
// across the full 72h span, which (a) never reached anything past yellow, so
// "72h old" and "6h old" were both indistinguishable shades of orange-red in
// the middle of the range, and (b) couldn't represent >72h detections as
// anything other than that same washed-out yellow. Four visually distinct
// hues instead of one continuous gradient make both the recent/stale
// contrast AND a fire's spread direction (red edge = where it's currently
// active) easier to read at a glance - matching RECENCY_LEGEND below.
const RECENCY_LEGEND = [
  { maxHours: 12, color: "#ef4444", label: "< 12 h" },
  { maxHours: 24, color: "#f97316", label: "12-24 h" },
  { maxHours: 48, color: "#eab308", label: "24-48 h" },
  { maxHours: 72, color: "#3b82f6", label: "48-72 h" },
];
const RECENCY_STALE_COLOR = "#6b7280"; // older than the oldest bucket (72h+) - clearly "cold", not another shade of the active-fire palette

function recencyColor(acquiredAtIso) {
  const ageHours = (Date.now() - new Date(acquiredAtIso).getTime()) / 3600000;
  const bucket = RECENCY_LEGEND.find((b) => ageHours <= b.maxHours);
  return bucket ? bucket.color : RECENCY_STALE_COLOR;
}

// Groups nearby point detections into a grid cell purely to cap how many SVG
// dots get drawn at country/region-wide zoom levels (lastFires isn't
// viewport-filtered, so a wide view can hold several thousand detections).
// This is a PERFORMANCE decimation, not a visual "bigger cluster = bigger
// circle" encoding - every rendered dot still gets the same fixed radius (see
// HOTSPOT_DOT_RADIUS) and is colored by its bucket's most recent detection, so
// dot size never hints at detection count. Grid size shrinks as you zoom in,
// so a close-up view separates out individual hotspots instead of always
// merging them into the same few blobs, until INDIVIDUAL_DOT_ZOOM removes
// bucketing altogether and plots every raw detection.
function gridDegForZoom(zoom) {
  if (zoom >= 13) return 0.01;
  if (zoom >= 11) return 0.02;
  if (zoom >= 9) return 0.05;
  if (zoom >= 7) return 0.1;
  return 0.3;
}

function clusterPointFires(fires, gridDeg) {
  const buckets = new Map();
  fires.forEach((fire) => {
    const key = `${Math.round(fire.latitude / gridDeg)}_${Math.round(fire.longitude / gridDeg)}`;
    if (!buckets.has(key)) {
      buckets.set(key, { points: [], mostRecent: fire.acquired_at });
    }
    const bucket = buckets.get(key);
    bucket.points.push([fire.latitude, fire.longitude]);
    if (new Date(fire.acquired_at) > new Date(bucket.mostRecent)) {
      bucket.mostRecent = fire.acquired_at;
    }
  });
  return Array.from(buckets.values()).map((bucket) => {
    const latSum = bucket.points.reduce((sum, p) => sum + p[0], 0);
    const lonSum = bucket.points.reduce((sum, p) => sum + p[1], 0);
    return {
      latitude: latSum / bucket.points.length,
      longitude: lonSum / bucket.points.length,
      count: bucket.points.length,
      acquired_at: bucket.mostRecent,
      points: bucket.points,
    };
  });
}

// Above this zoom, stop bucketing detections into grid-cell dots and plot
// each raw hotspot as its own small dot instead (matching how other
// fire-monitoring maps - e.g. Bseed WATCH - render FIRMS data: individual
// points colored by age, not merged blobs). Bucketing hides the actual
// density/shape texture of a fire's hotspot pattern; at this zoom there's
// enough screen space to show every point without them fully overlapping.
const INDIVIDUAL_DOT_ZOOM = 13;

// Groups fires by real-world proximity (chain-linkage union-find), not by a
// fixed grid cell - so hotspots that belong to the same spreading fire merge
// into ONE region regardless of how the display grid happens to slice them.
// This is what backs the affected-area polygon and its single geocode button,
// as opposed to clusterPointFires() above which is purely a zoom-dependent
// dot-count decimation for rendering performance.
const REGION_LINK_DEG = 0.03; // ~3km: hotspots this close are treated as the same fire event

// Mirrors the backend's INCIDENT_REASSOCIATION_DEG (services/incidents.py) -
// a single FireIncident can now be made of multiple spatially-separate raw
// clusters (a fire spotting/jumping several km between rebuild passes), so
// its centroid can legitimately sit further than REGION_LINK_DEG from any
// ONE of its own visual sub-polygons. Matching a rendered polygon to its
// backend incident needs the same wider radius, or a jumped fire's
// sub-polygons would stop finding their incident (no popup data, no growth
// estimate) purely because the incident's centroid drifted toward the
// midpoint between its now-merged parts.
const INCIDENT_REASSOCIATION_DEG = 0.15;

// Chain-linkage clustering (single-linkage) can connect two visually distant
// blobs into one group via a sparse "stepping stone" path of intermediate
// points, even when there's a real empty gap between them - and since a
// single concave hull can only describe ONE connected shape, it's then
// forced to draw an artificial thin bridge/isthmus across that gap to
// include every point. REGION_LINK_DEG stays as-is for incident identity
// (matches the backend's FireIncident clustering, and a fire that jumped a
// gap is still meaningfully "one incident"), but polygon DRAWING re-clusters
// each incident's points at this tighter distance, so a real gap renders as
// two separate polygons instead of one bridged shape. Tuned empirically
// against a real bridged incident (Asín, Zaragoza - a 122-detection group
// with two dense patches ~4km apart joined by a handful of stray points):
// REGION_LINK_DEG/2 (0.015) still chained all 122 into one shape; 0.007
// (~780m, comfortably above VIIRS' ~375m pixel spacing so a genuinely
// continuous burn front stays intact - confirmed on Bédar's 745-detection
// blob, which barely fragments at this threshold) correctly separated it
// into its real sub-clusters.
const HULL_SUBCLUSTER_DEG = 0.007;

function groupFiresByProximity(fires, thresholdDeg) {
  const n = fires.length;
  const parent = Array.from({ length: n }, (_, i) => i);
  function find(x) {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]];
      x = parent[x];
    }
    return x;
  }
  function union(a, b) {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent[ra] = rb;
  }
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const dLat = fires[i].latitude - fires[j].latitude;
      const dLon = fires[i].longitude - fires[j].longitude;
      if (Math.hypot(dLat, dLon) <= thresholdDeg) union(i, j);
    }
  }
  const groups = new Map();
  for (let i = 0; i < n; i++) {
    const root = find(i);
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root).push(fires[i]);
  }
  return Array.from(groups.values());
}

// How tightly the fire-area outline hugs its hotspots, in degrees (hull.js's
// edge-length threshold - lower = more concave). Tuned empirically against
// the Bédar/Almería incident (745 detections): 0.05 stayed ~90% of the
// convex hull's area (barely different), 0.02 ~70%, 0.01 visibly hugs the
// actual cluster shape with real concave notches without getting noisy/spiky.
const CONCAVE_HULL_CONCAVITY_DEG = 0.01;

// Concave hull (k-nearest-neighbors, via the hull.js library) - hugs the
// actual spread of hotspots instead of a convex hull's outward bulge to
// every extreme point. Falls back to the convex hull below if hull.js
// throws or degenerates (e.g. near-collinear points), so a shape always renders.
function concaveHull(points) {
  try {
    const result = hull(points, CONCAVE_HULL_CONCAVITY_DEG);
    if (result && result.length >= 3) return result;
  } catch {
    // fall through to convex hull
  }
  return convexHull(points);
}

// Andrew's monotone chain convex hull. Points as [lat, lon]; treats the small
// area as locally flat, which is fine at cluster scale (a few km).
function convexHull(points) {
  const unique = Array.from(new Set(points.map((p) => p.join(",")))).map((s) =>
    s.split(",").map(Number)
  );
  if (unique.length < 3) return null;
  const sorted = unique.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [];
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) {
      lower.pop();
    }
    lower.push(p);
  }
  const upper = [];
  for (let i = sorted.length - 1; i >= 0; i--) {
    const p = sorted[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) {
      upper.pop();
    }
    upper.push(p);
  }
  upper.pop();
  lower.pop();
  const hull = lower.concat(upper);
  return hull.length >= 3 ? hull : null;
}

// Shoelace formula, in squared-degrees (fine for the compactness ratio below -
// we only compare it against the same polygon's own perimeter, never convert
// it to a real-world area).
function polygonArea(points) {
  let sum = 0;
  for (let i = 0; i < points.length; i++) {
    const [lat1, lon1] = points[i];
    const [lat2, lon2] = points[(i + 1) % points.length];
    sum += lat1 * lon2 - lat2 * lon1;
  }
  return Math.abs(sum) / 2;
}

function polygonPerimeter(points) {
  let sum = 0;
  for (let i = 0; i < points.length; i++) {
    const [lat1, lon1] = points[i];
    const [lat2, lon2] = points[(i + 1) % points.length];
    sum += Math.hypot(lat2 - lat1, lon2 - lon1);
  }
  return sum;
}

// A handful of chain-linked points that are individually close enough to
// pass HULL_SUBCLUSTER_DEG, but spread out along a line, still forms a valid
// triangle/polygon - just a degenerate needle-thin one (near-zero area for
// its perimeter), which is exactly the "artificial bridge" look this guards
// against. Polsby-Popper compactness (4*pi*area / perimeter^2) is 1.0 for a
// circle and drops toward 0 for a sliver; a real fire-shaped blob comfortably
// clears this even when it's fairly elongated (e.g. a valley-following fire).
const MIN_HULL_COMPACTNESS = 0.06;

function isHullReasonablyCompact(hull) {
  const perimeter = polygonPerimeter(hull);
  if (perimeter === 0) return false;
  const compactness = (4 * Math.PI * polygonArea(hull)) / (perimeter * perimeter);
  return compactness >= MIN_HULL_COMPACTNESS;
}

// ---------- Hull smoothing + area/growth estimates (Turf.js) ----------
// Rounds off the jagged, dot-hugging look of a raw concave hull into
// something closer to a traced perimeter: a morphological "closing" (dilate
// then erode by the same distance) fills in small notches/spikes without
// eating real concave bays, which are typically much larger than this.
const SMOOTH_BUFFER_KM = 0.2;
const SMOOTH_SIMPLIFY_TOLERANCE_DEG = 0.0006;

function smoothRing(ringLatLon) {
  try {
    const coords = ringLatLon.map(([lat, lon]) => [lon, lat]); // turf uses [lon, lat]
    if (coords.length < 4) return ringLatLon;
    const grown = turf.buffer(turf.polygon([coords]), SMOOTH_BUFFER_KM, { units: "kilometers" });
    const shrunk = turf.buffer(grown, -SMOOTH_BUFFER_KM, { units: "kilometers" });
    const simplified = turf.simplify(shrunk, { tolerance: SMOOTH_SIMPLIFY_TOLERANCE_DEG, highQuality: true });

    let outRing = null;
    if (simplified.geometry.type === "Polygon") {
      outRing = simplified.geometry.coordinates[0];
    } else if (simplified.geometry.type === "MultiPolygon") {
      // A closing operation shouldn't split one blob into several, but
      // degenerate inputs might - keep only the largest piece.
      let best = null;
      for (const ringSet of simplified.geometry.coordinates) {
        const area = turf.area(turf.polygon(ringSet));
        if (!best || area > best.area) best = { ring: ringSet[0], area };
      }
      outRing = best ? best.ring : null;
    }
    if (!outRing || outRing.length < 4) return ringLatLon;
    return outRing.map(([lon, lat]) => [lat, lon]);
  } catch {
    return ringLatLon; // cosmetic refinement only - fall back to the raw hull on any failure
  }
}

function ringAreaHectares(ringLatLon) {
  try {
    const coords = ringLatLon.map(([lat, lon]) => [lon, lat]);
    if (coords.length < 4) return 0;
    return turf.area(turf.polygon([coords])) / 10000;
  } catch {
    return 0;
  }
}

function nearestPointPair(pointsA, pointsB) {
  let best = null;
  let bestDist = Infinity;
  for (const a of pointsA) {
    for (const b of pointsB) {
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      if (d < bestDist) {
        bestDist = d;
        best = [a, b];
      }
    }
  }
  return { pair: best, dist: bestDist };
}

// Connects an incident's spatially-separate fragments (each a raw
// REGION_LINK_DEG proximityGroup - see renderMap) with a dashed line, via a
// minimum spanning tree over nearest-point-pair distances so 3+ scattered
// fragments get the fewest/shortest connectors instead of an all-pairs
// tangle. Deliberately dashed/thin/lower-opacity, NOT a filled shape - this
// is an honest "probably spread through here, no satellite pass caught it"
// signal, not a claim about the actual burnt area in the gap.
function drawIncidentConnectors(incidentFragmentPoints) {
  incidentFragmentPoints.forEach((fragments) => {
    if (fragments.length < 2) return;
    const edges = [];
    for (let i = 0; i < fragments.length; i++) {
      for (let j = i + 1; j < fragments.length; j++) {
        const { pair, dist } = nearestPointPair(fragments[i], fragments[j]);
        if (pair) edges.push({ i, j, pair, dist });
      }
    }
    edges.sort((a, b) => a.dist - b.dist);

    const parent = fragments.map((_, i) => i);
    const find = (x) => (parent[x] === x ? x : (parent[x] = find(parent[x])));

    edges.forEach(({ i, j, pair }) => {
      const ri = find(i);
      const rj = find(j);
      if (ri === rj) return; // already connected (directly or transitively) - skip, avoids redundant/crossing lines
      parent[ri] = rj;
      L.polyline(pair, {
        color: HOTSPOT_STROKE,
        weight: 2,
        dashArray: "3,7",
        opacity: 0.55,
      })
        .bindTooltip("Posible trayectoria de propagación - sin detecciones vía satélite en este tramo", { sticky: true })
        .addTo(markersLayer);
    });
  });
}

// How many recent hours count as "the growth window" - the fire's area now
// vs its area as of GROWTH_WINDOW_HOURS ago (rebuilding the hull from only
// the older detections) gives a rough ha/hour growth rate. Thresholds are a
// POC-level heuristic (like the existing severity/risk scoring), not
// calibrated against real fire behavior statistics.
const GROWTH_WINDOW_HOURS = 3;
const GROWTH_FAST_HA_PER_HOUR = 5;
const GROWTH_MODERATE_HA_PER_HOUR = 0.5;

function estimateIncidentGrowth(group) {
  const points = group.map((f) => [f.latitude, f.longitude]);
  const nowHull = group.length >= 3 ? concaveHull(points) : null;
  const areaNowHa = nowHull ? ringAreaHectares(smoothRing(nowHull)) : 0;
  // Raw timestamps kept alongside the growth stats so the incident detail
  // view can draw a detections-over-time sparkline without a second fetch.
  const timestamps = group.map((f) => f.acquired_at);

  const cutoffMs = Date.now() - GROWTH_WINDOW_HOURS * 3600000;
  const priorGroup = group.filter((f) => new Date(f.acquired_at).getTime() <= cutoffMs);
  if (priorGroup.length < 3) {
    // Not enough history to compare against - the fire (or at least this
    // detected extent of it) is younger than the growth window itself.
    return { areaHa: areaNowHa, rateHaPerHour: null, level: "new", timestamps };
  }
  const priorPoints = priorGroup.map((f) => [f.latitude, f.longitude]);
  const priorHull = concaveHull(priorPoints);
  const areaPriorHa = priorHull ? ringAreaHectares(smoothRing(priorHull)) : 0;

  const rateHaPerHour = (areaNowHa - areaPriorHa) / GROWTH_WINDOW_HOURS;
  let level = "stable";
  if (rateHaPerHour > GROWTH_FAST_HA_PER_HOUR) level = "fast";
  else if (rateHaPerHour > GROWTH_MODERATE_HA_PER_HOUR) level = "moderate";
  return { areaHa: areaNowHa, rateHaPerHour, level, timestamps };
}

const GROWTH_LABELS = {
  new: { label: "Reciente", className: "growth-new" },
  stable: { label: "Estable", className: "growth-stable" },
  moderate: { label: "Creciendo", className: "growth-moderate" },
  fast: { label: "Creciendo rápido", className: "growth-fast" },
};

function growthBadgeHtml(growth) {
  if (!growth) return "";
  const info = GROWTH_LABELS[growth.level];
  return `<span class="growth-badge ${info.className}">${info.label}</span>`;
}

function areaSummaryHtml(growth) {
  if (!growth || growth.areaHa < 0.1) return "";
  const acres = growth.areaHa * 2.47105;
  return (
    `${growth.areaHa.toLocaleString("es-ES", { maximumFractionDigits: 1 })} ha` +
    ` (${acres.toLocaleString("es-ES", { maximumFractionDigits: 1 })} acres, estimado)`
  );
}

// Daily activity chart: new detections PER CALENDAR DAY across the
// incident's FULL lifetime (first detection -> now), not a fixed 48h/4h
// window - a fire active 10 days showed almost nothing in the old 48h
// sparkline, which made a slow-burning multi-week incident look brand new.
// Bucketing by day (not hour) directly answers "how did this grow day by
// day" rather than an hour-granularity view nobody asked for.
//
// Built from the incident's own timeline events (event_type "detection"),
// which are already fetched in full (unfiltered by the map's date-range
// selector - see showIncidentDetail) for the chronology list - reusing that
// same data here avoids a second network round-trip.
const DAILY_CHART_WIDTH = 280;
const DAILY_CHART_HEIGHT = 60;
const DAILY_CHART_MAX_BARS = 21; // ~3 weeks before bars get too thin to read; INCIDENTS_WINDOW_HOURS caps real incidents at 30 days anyway

// The current backend templates (services/incidents.py) both start a
// "detection" event's TITLE with the count - "N detección(es) nueva(s)" or
// "N detección(es) en el cluster inicial." A handful of older incidents in
// the DB predate that copy (confirmed live: incident 234 has an event titled
// plain "First detection", with the count only in its DESCRIPTION -
// "122 detection(s) in the initial cluster.") - checking description too
// means those older rows still count instead of silently vanishing from the
// very first day of the chart.
function detectionEventCount(event) {
  const fromTitle = /^(\d+)/.exec(event.title || "");
  if (fromTitle) return Number(fromTitle[1]);
  const fromDescription = /^(\d+)/.exec(event.description || "");
  return fromDescription ? Number(fromDescription[1]) : 0;
}

function dailyDetectionCounts(events) {
  const perDay = new Map(); // "YYYY-MM-DD" -> count
  events
    .filter((e) => e.event_type === "detection")
    .forEach((e) => {
      const day = e.occurred_at.slice(0, 10);
      perDay.set(day, (perDay.get(day) || 0) + detectionEventCount(e));
    });
  if (perDay.size === 0) return [];

  // Fill in zero-count days between the first and last so gaps in activity
  // are visible as gaps, not silently skipped/compressed out of the axis.
  const days = Array.from(perDay.keys()).sort();
  const first = new Date(days[0] + "T00:00:00Z");
  const last = new Date(days[days.length - 1] + "T00:00:00Z");
  const series = [];
  for (let d = new Date(first); d <= last; d.setUTCDate(d.getUTCDate() + 1)) {
    const key = d.toISOString().slice(0, 10);
    series.push({ day: key, count: perDay.get(key) || 0 });
  }
  // Long-running incidents (up to INCIDENTS_WINDOW_HOURS = 30 days) would
  // otherwise render unreadably thin bars - keep only the most recent
  // DAILY_CHART_MAX_BARS days rather than silently mis-scaling every bar.
  return series.slice(-DAILY_CHART_MAX_BARS);
}

function dayLabel(dayKey) {
  return new Date(dayKey + "T00:00:00Z").toLocaleDateString("es-ES", { day: "numeric", month: "short" });
}

// Reserved headroom above the bars for the peak day's direct value label -
// selective (only the peak, not every bar) per the app's dataviz conventions.
const DAILY_CHART_LABEL_HEADROOM = 16;
const DAILY_CHART_BAR_AREA = DAILY_CHART_HEIGHT - DAILY_CHART_LABEL_HEADROOM;

function dailyActivityChartHtml(events) {
  const series = dailyDetectionCounts(events);
  if (series.length < 2) return "";

  const maxCount = Math.max(...series.map((d) => d.count), 1);
  const peakIndex = series.reduce((best, d, i) => (d.count > series[best].count ? i : best), 0);
  const barGap = 3;
  const barWidth = DAILY_CHART_WIDTH / series.length - barGap;
  const todayKey = new Date().toISOString().slice(0, 10);

  const bars = series
    .map((d, i) => {
      // Minimum visible height even for a 0-count day - a bar that's
      // literally invisible reads as "no data" (a rendering gap), not "zero
      // activity that day", which is itself meaningful information here.
      const barHeight = Math.max(3, (d.count / maxCount) * DAILY_CHART_BAR_AREA);
      const x = i * (barWidth + barGap);
      const y = DAILY_CHART_HEIGHT - barHeight;
      // Today's bar (or the most recent day with data) stays full-strength;
      // earlier days step down in opacity - the same "recent = stronger
      // signal" language the map's own recency colors already use, applied
      // here as intensity instead of hue since this is one series, not a
      // category per bar.
      const isLatest = d.day === todayKey || i === series.length - 1;
      const opacity = isLatest ? 1 : 0.45 + 0.4 * (i / (series.length - 1));
      const peakLabel =
        i === peakIndex && d.count > 0
          ? `<text x="${(x + barWidth / 2).toFixed(1)}" y="${Math.max(10, y - 5).toFixed(1)}" text-anchor="middle" class="daily-chart-peak-label">${d.count}</text>`
          : "";
      return (
        `<rect class="daily-chart-bar" data-label="${dayLabel(d.day)}" data-count="${d.count}" ` +
        `x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" ` +
        `fill="var(--accent)" opacity="${opacity.toFixed(2)}" rx="2.5"/>` +
        peakLabel
      );
    })
    .join("");

  const firstLabel = dayLabel(series[0].day);
  const lastLabel = dayLabel(series[series.length - 1].day);

  return (
    `<div class="sparkline-wrap">` +
    `<div class="sparkline-label">Detecciones por día</div>` +
    `<svg viewBox="0 0 ${DAILY_CHART_WIDTH} ${DAILY_CHART_HEIGHT}" class="sparkline-svg daily-chart-svg" preserveAspectRatio="none">` +
    `<line x1="0" y1="${DAILY_CHART_HEIGHT - 0.5}" x2="${DAILY_CHART_WIDTH}" y2="${DAILY_CHART_HEIGHT - 0.5}" stroke="var(--border-soft)" stroke-width="1"/>` +
    bars +
    `</svg>` +
    `<div class="recency-labels"><span>${firstLabel}</span><span>${lastLabel}</span></div>` +
    `</div>`
  );
}

async function loadConfig() {
  const res = await fetch("/config");
  const data = await res.json();
  apiBaseUrl = data.apiBaseUrl;
}

function getSelectedDays() {
  return Number(document.getElementById("date-range").value);
}

let lastFires = [];
let lastReports = [];

// Client-side cache of geocode lookups, on top of the backend's own DB cache
// (locality_cache) - so re-rendering on zoom/refresh never re-fetches a name
// we already have in this browser session, not even from our own backend.
const geocodeCache = new Map();

// Burnt-area/growth estimate per incident id, refreshed on every renderMap()
// pass (see estimateIncidentGrowth) - the sidebar's incident detail view
// doesn't have the raw per-point group itself, so it looks results up here
// instead of recomputing them.
const incidentEstimatesById = new Map();

function geocodeCacheKey(lat, lon) {
  return `${lat.toFixed(2)},${lon.toFixed(2)}`;
}

async function getGeocode(lat, lon) {
  const key = geocodeCacheKey(lat, lon);
  if (geocodeCache.has(key)) return geocodeCache.get(key);
  const res = await fetch(`${apiBaseUrl}/api/geocode?lat=${lat}&lon=${lon}`);
  const data = await res.json();
  geocodeCache.set(key, data);
  return data;
}

const X_LOGO_SVG =
  '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" style="vertical-align:-2px;">' +
  '<path fill="currentColor" d="M18.24 2.25h3.31l-7.23 8.26 8.5 11.24h-6.66l-5.21-6.82-5.97 6.82H1.66l7.73-8.84L1.24 2.25h6.83l4.71 6.23z"/>' +
  "</svg>";

// Monochrome line icons (replacing colorful emoji in dynamically-generated
// popup/card HTML) - inherit currentColor from whatever text surrounds them,
// same rationale as the .icon class used in index.html's static markup.
const ICON_SVG_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  flame:
    `<svg ${ICON_SVG_ATTRS} width="13" height="13" class="icon"><path d="M12 2c1 3-3 4-3 8a3 3 0 0 0 6 0c0-1-.5-2-1-2.5.5 2 .5 4-1 5.5a4 4 0 0 1-4-4c0-3 2-4 2-7-2 1-4 4-4 7a5 5 0 0 0 10 0c0-5-3-6-5-7z"/></svg>`,
  clock:
    `<svg ${ICON_SVG_ATTRS} width="12" height="12" class="icon"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
  shield:
    `<svg ${ICON_SVG_ATTRS} width="13" height="13" class="icon"><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/></svg>`,
  satellite:
    `<svg ${ICON_SVG_ATTRS} width="14" height="14" class="icon"><path d="M13 7l4 4-6 6-4-4a5.66 5.66 0 0 1 6-6z"/><path d="M3 21l3.5-3.5"/><path d="M17 3a11 11 0 0 1 4 4M14 6a7 7 0 0 1 4 4"/></svg>`,
  send:
    `<svg ${ICON_SVG_ATTRS} width="13" height="13" class="icon"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>`,
  droplet:
    `<svg ${ICON_SVG_ATTRS} width="13" height="13" class="icon"><path d="M12 3s6 6.5 6 11a6 6 0 1 1-12 0c0-4.5 6-11 6-11z"/></svg>`,
  camera:
    `<svg ${ICON_SVG_ATTRS} width="14" height="14" class="icon"><path d="M4 8h3l1.5-2h7L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.2"/></svg>`,
};

// Cache of Telegram messages matched to an incident (parallel to
// geocodeCache below) so re-opening the same polygon's popup doesn't re-fetch.
const telegramCache = new Map();

async function getTelegramMentions(incidentId) {
  if (telegramCache.has(incidentId)) return telegramCache.get(incidentId);
  const res = await fetch(`${apiBaseUrl}/api/telegram/messages?incident_id=${incidentId}`);
  const data = await res.json();
  telegramCache.set(incidentId, data);
  return data;
}

// Cache of official regional-government status records matched to an
// incident (parallel to telegramCache) so re-opening the same polygon's
// popup doesn't re-fetch.
const regionalCache = new Map();

async function getRegionalStatus(incidentId) {
  if (regionalCache.has(incidentId)) return regionalCache.get(incidentId);
  const res = await fetch(`${apiBaseUrl}/api/regional-incidents?incident_id=${incidentId}`);
  const data = await res.json();
  regionalCache.set(incidentId, data);
  return data;
}

// Mirrors the tone of the backend's _personnel_description
// (app/services/regional_incidents/sync.py): "N resource(s) deployed
// (breakdown)". personnel_summary arrives as a JSON string field.
function personnelDescription(personnelSummaryJson) {
  if (!personnelSummaryJson) return null;
  let summary;
  try {
    summary = JSON.parse(personnelSummaryJson);
  } catch {
    return null;
  }
  const total = summary.total_actuando || 0;
  if (!total) return null;
  const breakdown = Object.entries(summary)
    .filter(([key, count]) => key !== "total_actuando" && count)
    .map(([key, count]) => `${count} ${key}`)
    .join(", ");
  return `${total} medio${total === 1 ? "" : "s"} desplegado${total === 1 ? "" : "s"}` + (breakdown ? ` (${breakdown})` : "");
}

function regionalSectionHtml(records) {
  if (!records || records.length === 0) return "";
  // Several regional records can match the same incident (e.g. adjoining
  // municipalities) - show the most recently updated as the representative status.
  const latest = records.slice().sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at))[0];
  const personnel = personnelDescription(latest.personnel_summary);
  const place = [latest.municipality, latest.province].filter(Boolean).join(" · ");
  return (
    `<div class="regional-card">` +
    `<div class="regional-card-title">${ICONS.shield} Estado oficial <span class="regional-status-pill">${latest.status}</span></div>` +
    (place ? `<div class="regional-summary">${place}</div>` : "") +
    (personnel ? `<div class="regional-summary">${personnel}</div>` : "") +
    `</div>`
  );
}

// Cache of Sentinel scenes matched to an incident (parallel to
// telegramCache/regionalCache) so re-opening the same polygon's popup
// doesn't re-fetch the scene list (though the thumbnail image itself is
// still fetched/cached separately by the browser via its own <img> src).
const satelliteCache = new Map();

async function getSatelliteScenes(incidentId) {
  if (satelliteCache.has(incidentId)) return satelliteCache.get(incidentId);
  const res = await fetch(`${apiBaseUrl}/api/copernicus/scenes?incident_id=${incidentId}`);
  const data = await res.json();
  satelliteCache.set(incidentId, data);
  return data;
}

function satelliteSectionHtml(scenes) {
  if (!scenes || scenes.length === 0) return "";
  // Prefer the clearest (lowest cloud cover) scene as the representative
  // thumbnail - a 95%-cloud scene is useless as a "what does the area look
  // like" preview even if it's the most recent one.
  const best = scenes.slice().sort((a, b) => (a.cloud_cover ?? 100) - (b.cloud_cover ?? 100))[0];
  const thumbUrl = `${apiBaseUrl}/api/copernicus/scenes/${best.id}/thumbnail`;
  const capturedDate = best.captured_at ? best.captured_at.slice(0, 10) : "";
  return (
    `<div class="satellite-card">` +
    `<div class="satellite-card-title">${ICONS.satellite} ${scenes.length} escena${scenes.length > 1 ? "s" : ""} de satélite</div>` +
    `<img src="${thumbUrl}" class="satellite-thumb" />` +
    `<div class="satellite-summary">${capturedDate}` +
    (best.cloud_cover != null ? ` · ${best.cloud_cover.toFixed(0)}% nubes` : "") +
    `</div>` +
    `</div>`
  );
}

function telegramSectionHtml(messages) {
  if (!messages || messages.length === 0) return "";
  const withPhoto = messages.find((m) => m.media_path);
  const thumb = withPhoto
    ? `<img src="${apiBaseUrl}/media/${encodeURIComponent(withPhoto.media_path)}" class="telegram-thumb" />`
    : "";
  const latest = messages[0];
  return (
    `<div class="telegram-card">` +
    `<div class="telegram-card-title">${ICONS.send} ${messages.length} mención${messages.length > 1 ? "es" : ""} en Telegram</div>` +
    thumb +
    (latest.text ? `<div class="telegram-snippet">${latest.text.slice(0, 140)}</div>` : "") +
    `</div>`
  );
}

function regionPopupHtml(
  group,
  earliest,
  mostRecent,
  geo,
  matchedIncident,
  telegramMessages,
  regionalRecords,
  satelliteScenes,
  growth
) {
  // Prefer the matched incident's own canonical (sticky, backend-resolved)
  // name over a fresh per-point reverse-geocode of wherever THIS particular
  // visual sub-shape/marker happens to sit - an incident can now legitimately
  // span multiple towns (see INCIDENT_REASSOCIATION_DEG: a fire that jumps
  // several km still counts as one incident), and showing each fragment's
  // own nearest-town name instead of the incident's name is exactly the
  // confusing "La Mierla" vs "Arbancón" split confirmed live this session -
  // same fire, two different popup titles depending which part you clicked.
  const title = matchedIncident && matchedIncident.locality ? matchedIncident.locality : geo ? geo.locality : "Área de incendio estimada";
  const subtitle =
    matchedIncident && matchedIncident.province
      ? `<span class="card-subtitle"> · ${matchedIncident.province}</span>`
      : geo && geo.province
      ? `<span class="card-subtitle"> · ${geo.province}</span>`
      : "";
  // Prefer the incident's own official area_ha (EFFIS) when it has one -
  // more accurate than our hotspot-hull estimate; the estimate is the
  // fallback (and is what growth-rate comparisons here are always based on,
  // since EFFIS doesn't publish a time series to diff against).
  const officialAreaHa = matchedIncident && matchedIncident.area_ha != null ? matchedIncident.area_ha : null;
  const areaLine =
    officialAreaHa != null
      ? `Área quemada &nbsp;${officialAreaHa.toLocaleString("es-ES", { maximumFractionDigits: 1 })} ha (oficial)`
      : areaSummaryHtml(growth)
      ? `Área quemada &nbsp;${areaSummaryHtml(growth)}`
      : "";
  const meta =
    `<div class="card-meta">` +
    `${group.length} detecci${group.length > 1 ? "ones" : "ón"}<br/>` +
    `Primera detección &nbsp;${earliest.acquired_at}<br/>` +
    `Más reciente &nbsp;${mostRecent}` +
    (areaLine ? `<br/>${areaLine}` : "") +
    `</div>` +
    (growth ? `<div style="margin-top:6px;">${growthBadgeHtml(growth)}</div>` : "") +
    `<span class="card-caveat">Basado en la dispersión de focos, no es un perímetro oficial</span>`;

  const timelineBtn = matchedIncident
    ? `<div style="margin-top:10px;"><button class="timeline-btn">Ver cronología del incendio &rarr;</button></div>`
    : "";
  const regionalSection = regionalSectionHtml(regionalRecords);
  const telegramSection = telegramSectionHtml(telegramMessages);
  const satelliteSection = satelliteSectionHtml(satelliteScenes);

  if (geo) {
    const searchUrl = `https://x.com/search?q=${encodeURIComponent(geo.hashtag)}&src=typed_query&f=live`;
    return (
      `<div class="card-title">${title}${subtitle}</div>${meta}` +
      `<div class="x-card">` +
      `<code>${geo.hashtag}</code> <button class="copy-btn" data-hashtag="${geo.hashtag}">Copiar</button>` +
      `<a class="x-link" href="${searchUrl}" target="_blank" rel="noopener">${X_LOGO_SVG}<span>Buscar en X</span></a>` +
      `</div>` +
      regionalSection +
      satelliteSection +
      telegramSection +
      timelineBtn
    );
  }

  return (
    `<div class="card-title">${title}</div>${meta}` +
    `<div style="margin-top:10px;"><button class="geocode-btn">Obtener ubicación y hashtag</button></div>` +
    `<div class="geocode-result" style="margin-top:6px;"></div>` +
    regionalSection +
    satelliteSection +
    telegramSection +
    timelineBtn
  );
}

// Attaches (and auto-resolves) the location/hashtag for a region polygon.
// Renames the polygon's popup title and adds a persistent hover label once
// resolved, from cache if we already have it, or fetched in the background
// so it appears without requiring a click - the manual button stays as a
// fallback/retry if that background fetch fails. If this polygon's centroid
// matches a backend FireIncident (matchedIncident), also surfaces Telegram
// mentions/images for that fire and a button into its full event timeline.
function attachGeocode(polygon, group, earliest, mostRecent, matchedIncident, growth) {
  let geo = geocodeCache.get(geocodeCacheKey(earliest.latitude, earliest.longitude)) || null;
  let telegramMessages = matchedIncident ? telegramCache.get(matchedIncident.id) || null : null;
  let regionalRecords =
    matchedIncident && matchedIncident.has_regional_status
      ? regionalCache.get(matchedIncident.id) || null
      : null;
  let satelliteScenes =
    matchedIncident && matchedIncident.has_satellite_imagery
      ? satelliteCache.get(matchedIncident.id) || null
      : null;

  const render = () =>
    regionPopupHtml(
      group,
      earliest,
      mostRecent,
      geo,
      matchedIncident,
      telegramMessages,
      regionalRecords,
      satelliteScenes,
      growth
    );
  // Same "prefer the incident's own canonical name" rule as regionPopupHtml's
  // title - keeps the hover tooltip consistent with the popup instead of
  // showing a different per-point nearest-town name for a fire that spans
  // more than one.
  const displayLocality = () => (matchedIncident && matchedIncident.locality ? matchedIncident.locality : geo && geo.locality);

  polygon.bindPopup(render());
  if (displayLocality()) polygon.bindTooltip(displayLocality(), { sticky: true });

  polygon.on("popupopen", (e) => {
    const container = e.popup.getElement();
    const copyBtn = container.querySelector(".copy-btn");
    if (copyBtn) {
      copyBtn.onclick = () => navigator.clipboard.writeText(copyBtn.dataset.hashtag);
    }
    const timelineBtn = container.querySelector(".timeline-btn");
    if (timelineBtn && matchedIncident) {
      timelineBtn.onclick = () => {
        map.closePopup();
        showIncidentDetail(matchedIncident);
      };
    }
    const btn = container.querySelector(".geocode-btn");
    if (!btn) return;
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "Buscando...";
      try {
        geo = await getGeocode(earliest.latitude, earliest.longitude);
        polygon.setPopupContent(render());
        polygon.bindTooltip(displayLocality(), { sticky: true });
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Obtener ubicación y hashtag";
      }
    };
  });

  if (!geo) {
    getGeocode(earliest.latitude, earliest.longitude)
      .then((data) => {
        geo = data;
        polygon.setPopupContent(render());
        polygon.bindTooltip(displayLocality(), { sticky: true });
      })
      .catch(() => {}); // manual button above still works as a retry
  }

  if (matchedIncident && telegramMessages === null) {
    getTelegramMentions(matchedIncident.id)
      .then((data) => {
        telegramMessages = data;
        polygon.setPopupContent(render());
      })
      .catch(() => {});
  }

  if (matchedIncident && matchedIncident.has_regional_status && regionalRecords === null) {
    getRegionalStatus(matchedIncident.id)
      .then((data) => {
        regionalRecords = data;
        polygon.setPopupContent(render());
      })
      .catch(() => {});
  }

  if (matchedIncident && matchedIncident.has_satellite_imagery && satelliteScenes === null) {
    getSatelliteScenes(matchedIncident.id)
      .then((data) => {
        satelliteScenes = data;
        polygon.setPopupContent(render());
      })
      .catch(() => {});
  }
}

// Below this zoom, overlapping big circles get unreadable - show only the
// region shapes (with a date gradient) instead of clustered circles.
const LOW_ZOOM_POLYGON_ONLY = 8;

// Injects/replaces a <linearGradient> in the map's SVG so a polygon's fill
// can go from one color to another instead of a single flat color.
function ensureLinearGradient(svgRoot, id, stops) {
  if (!svgRoot) return false;
  let defs = svgRoot.querySelector("defs");
  if (!defs) {
    defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    svgRoot.insertBefore(defs, svgRoot.firstChild);
  }
  const existing = defs.querySelector(`#${id}`);
  if (existing) existing.remove();
  const gradient = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
  gradient.setAttribute("id", id);
  gradient.setAttribute("x1", "0%");
  gradient.setAttribute("y1", "0%");
  gradient.setAttribute("x2", "100%");
  gradient.setAttribute("y2", "100%");
  stops.forEach(([offset, color]) => {
    const stop = document.createElementNS("http://www.w3.org/2000/svg", "stop");
    stop.setAttribute("offset", `${offset}%`);
    stop.setAttribute("stop-color", color);
    gradient.appendChild(stop);
  });
  defs.appendChild(gradient);
  return true;
}

// Red (latest detection in this group) -> blue (earliest), matching the same
// RECENCY_LEGEND endpoints the dots use (red = most recent, blue = ~72h old)
// so the polygon backdrop and the dots on top of it read as one consistent
// color language. Scaled to the group's OWN date range rather than the
// display window - so a fire spanning just a couple of days still shows
// visible contrast, not near-identical hues.
function regionGradientStops(group) {
  const times = group.map((f) => new Date(f.acquired_at).getTime());
  const min = Math.min(...times);
  const max = Math.max(...times);
  if (min === max) {
    const solid = RECENCY_LEGEND[1].color; // single-timestamp group: flat mid-tone
    return [[0, solid], [100, solid]];
  }
  return [
    [0, RECENCY_LEGEND[0].color], // newest = red
    [100, RECENCY_LEGEND[RECENCY_LEGEND.length - 1].color], // oldest (within the group's own span) = blue
  ];
}

// Pure rendering pass over already-fetched data - re-run on zoom changes
// without re-hitting the API, since clustering depends on the current zoom.
function renderMap() {
  markersLayer.clearLayers();
  incidentEstimatesById.clear();
  const hours = getSelectedDays() * 24;
  const zoom = map.getZoom();
  const gridDeg = gridDegForZoom(zoom);
  const lowZoom = zoom < LOW_ZOOM_POLYGON_ONLY;

  // One entry per incident id -> array of point-lists, one per spatially-
  // separate fragment (each a raw-tight REGION_LINK_DEG proximityGroup - see
  // below). An incident whose fragments are far enough apart to render as
  // separate shapes gets a dashed connector line drawn between them after
  // the main loop (see drawIncidentConnectors below) - confirmed live this
  // session that leaving a stark visual gap between two parts of the SAME
  // fire (e.g. La Mierla's main blob + a satellite pass ~6km away a few
  // hours later) reads as "two unrelated fires" rather than one spreading
  // one.
  const incidentFragmentPoints = new Map();

  const pointFires = [];
  lastFires.forEach((fire) => {
    let geometry = null;
    if (fire.geometry_geojson) {
      try {
        geometry = JSON.parse(fire.geometry_geojson);
      } catch {
        geometry = null;
      }
    }

    if (geometry && (geometry.type === "Polygon" || geometry.type === "MultiPolygon")) {
      // Burnt-area perimeter: render the actual shape, not just a point.
      const fillColor = recencyColor(fire.acquired_at);
      const popupHtml =
        `<div class="card-title">Área quemada</div>` +
        `<div class="card-meta">Inicio &nbsp;${fire.acquired_at}<br/>` +
        (fire.area_ha != null ? `Área afectada &nbsp;${fire.area_ha.toLocaleString()} ha` : "") +
        `</div>`;
      L.geoJSON(geometry, {
        style: { color: HOTSPOT_STROKE, weight: 2, fillColor, fillOpacity: 0.5 },
      })
        .bindPopup(popupHtml)
        .addTo(markersLayer);
    } else {
      pointFires.push(fire);
    }
  });

  // One unified polygon (and one geocode button) per contiguous fire event,
  // grouped by real-world proximity - NOT per display grid cell, so it
  // doesn't fragment into dozens of small overlapping shapes that swallow
  // clicks meant for the markers on top of them.
  const proximityGroups = groupFiresByProximity(pointFires, REGION_LINK_DEG);
  const pointsInHullGroups = new Set();
  const filteredOutPoints = new Set();

  proximityGroups.forEach((group, idx) => {
    // Filtering must run BEFORE the <3-points hull bail-out below - a fire
    // with only 1-2 detections (common for a lower detection_count incident)
    // never reaches the hull-building code, so checking filters only after
    // that bail-out meant those small incidents' dots always rendered
    // regardless of the sidebar's risk/status checkboxes - confirmed live:
    // "Solo crítico + activo" narrowed the sidebar list correctly but left
    // plenty of non-matching dots showing on the map.
    const groupLat = group.reduce((sum, f) => sum + f.latitude, 0) / group.length;
    const groupLon = group.reduce((sum, f) => sum + f.longitude, 0) / group.length;
    const matchedIncident = findMatchingIncident(groupLat, groupLon);

    // Stretch goal: apply the same sidebar filters to matched polygons, so
    // narrowing to e.g. "Critical" in the sidebar also hides its map shape
    // (and, below, its underlying detections in the clustered/isolated-marker
    // views). Groups with no matched incident (no backend FireIncident to
    // filter on) always render, same as before this feature existed.
    if (matchedIncident && !incidentPassesFilters(matchedIncident, getActiveFilters())) {
      group.forEach((f) => filteredOutPoints.add(f));
      return;
    }

    if (group.length < 3) return; // need at least a triangle for a meaningful hull

    // Identity (name, timeline, matched incident, filters) is decided once
    // for the WHOLE chain-linked group - a fire that jumped a real gap is
    // still one incident. Only the polygon SHAPE is re-clustered below.
    const earliest = group.reduce((oldest, f) =>
      new Date(f.acquired_at) < new Date(oldest.acquired_at) ? f : oldest
    );
    const mostRecent = group.reduce(
      (latest, f) => (new Date(f.acquired_at) > new Date(latest) ? f.acquired_at : latest),
      group[0].acquired_at
    );
    group.forEach((f) => pointsInHullGroups.add(f));

    // Burnt-area estimate + growth trend computed once for the WHOLE incident
    // (not per visual sub-shape) - see estimateIncidentGrowth. Cached by
    // incident id so the sidebar detail view (which doesn't have the raw
    // point group) can look it up too.
    const growth = matchedIncident ? estimateIncidentGrowth(group) : null;
    if (matchedIncident && growth) incidentEstimatesById.set(matchedIncident.id, growth);

    // Records one fragment (a hull polygon OR a loose 1-2 point leftover -
    // see below) for the connector-drawing pass after the main loop. Pushing
    // per-SUBGROUP (not once per outer group) matters: a loose leftover that
    // fell just outside HULL_SUBCLUSTER_DEG of its own outer group's main
    // hull needs a connector just as much as a genuinely separate outer
    // group does - confirmed live (La Mierla: 2 stray points near Embalse de
    // Beleño stayed unconnected because they were still technically part of
    // the same outer group as the main polygon, just not its hull).
    const registerFragment = (points) => {
      if (!matchedIncident) return;
      if (!incidentFragmentPoints.has(matchedIncident.id)) incidentFragmentPoints.set(matchedIncident.id, []);
      incidentFragmentPoints.get(matchedIncident.id).push(points);
    };

    // Re-cluster at a tighter distance purely for drawing: see
    // HULL_SUBCLUSTER_DEG above. A real gap between two parts of the same
    // chain-linked fire renders as two (or more) separate polygons instead of
    // one hull bridged across empty space.
    const allSubgroups = groupFiresByProximity(group, HULL_SUBCLUSTER_DEG);
    const subgroups = allSubgroups.filter((sg) => sg.length >= 3);

    // Subgroups with only 1-2 points can't form a meaningful hull (a
    // triangle from 2 far-apart points is a degenerate sliver, not a real
    // shape) - this genuinely happens with MODIS detections (confirmed live:
    // La Mierla, Guadalajara had a satellite pass ~6km from its main cluster
    // whose 3-4 points sit ~900m-1km apart, just outside HULL_SUBCLUSTER_DEG
    // (~780m, tuned tight specifically to avoid the Asín bridging-artifact
    // regression - loosening it back up to fit MODIS' coarser spacing was
    // tried and rejected: at 0.01 deg it already re-merged 120 of Asín's 122
    // points into one bridged blob). Rather than force an unreliable shape,
    // these render as their own small highlighted markers, still linked to
    // the SAME incident's popup/growth data - so the map still visually
    // communicates "this is part of the same fire", just not as a polygon.
    const looseSubgroups = allSubgroups.filter((sg) => sg.length < 3);
    looseSubgroups.forEach((subgroup) => {
      registerFragment(subgroup.map((f) => [f.latitude, f.longitude]));
      subgroup.forEach((fire) => {
        const marker = L.circleMarker([fire.latitude, fire.longitude], {
          radius: HOTSPOT_DOT_RADIUS + 2,
          color: matchedIncident ? "#ff6a3d" : HOTSPOT_STROKE, // accent ring = "linked to an incident"
          weight: matchedIncident ? 2.5 : 1,
          fillColor: recencyColor(fire.acquired_at),
          fillOpacity: 0.9,
        });
        if (matchedIncident) {
          attachGeocode(marker, group, earliest, mostRecent, matchedIncident, growth);
        } else {
          marker.bindPopup(`<div class="card-meta">Detectado &nbsp;${fire.acquired_at}</div>`);
        }
        marker.addTo(markersLayer);
      });
    });

    subgroups.forEach((subgroup, subIdx) => {
      const points = subgroup.map((f) => [f.latitude, f.longitude]);
      const rawHull = concaveHull(points);
      if (!rawHull || !isHullReasonablyCompact(rawHull)) return;
      const hull = smoothRing(rawHull);
      registerFragment(points);

      // A thin dark outline (the old HOTSPOT_STROKE) reads fine against plain
      // OSM tiles, but gets visually lost once dot markers sit on top of it -
      // the polygon's extent stopped being readable at a glance. A bold white
      // halo underneath a solid, fairly thick dark line gives a contour that
      // reads against any basemap color AND against the dots sitting on top
      // of it. Now that dots render at a small FIXED radius everywhere (no
      // more size-scaled cluster circles obscuring the interior), the
      // polygon's fill is the primary "this is the fire's shape" signal at
      // every zoom, so it's kept bold and solid rather than fading out or
      // going dashed as you zoom in.
      const gradientId = `region-gradient-${idx}-${subIdx}`;
      const haloWeight = lowZoom ? 6 : 5.5;
      const strokeWeight = lowZoom ? 3 : 2.5;
      const halo = L.polygon(hull, {
        color: "#ffffff",
        weight: haloWeight,
        opacity: 0.9,
        fill: false,
      });
      halo.addTo(markersLayer);

      // Kept deliberately faint (vs. the old 0.48/0.62) - at full-zoom this
      // fill sits directly underneath the individual recency-colored dots
      // (see INDIVIDUAL_DOT_ZOOM below), and since both use the SAME red-to-
      // yellow hue scale, a strong fill made hot (red) dots blend into an
      // already-red backdrop - confirmed live: a dense recent cluster read as
      // one solid orange blob with no visible hot/cool contrast. A faint tint
      // still communicates "this is the fire's extent" without competing with
      // the dots for the same color signal - matching how Copernicus's own
      // EMSR grading maps use a flat, muted burnt-area fill with small vivid
      // point markers on top, rather than color-coding the fill itself.
      const polygon = L.polygon(hull, {
        color: HOTSPOT_STROKE,
        weight: strokeWeight,
        fillColor: "#888", // placeholder until the gradient is attached below
        fillOpacity: lowZoom ? 0.32 : 0.22,
      });
      // Popup/geocode reflects the whole incident's stats (earliest/most
      // recent/matched incident across the full chain-linked group), not
      // just this visual sub-shape's own slice of it.
      attachGeocode(polygon, group, earliest, mostRecent, matchedIncident, growth);
      polygon.addTo(markersLayer);

      const svgRoot = polygon.getElement() && polygon.getElement().ownerSVGElement;
      if (ensureLinearGradient(svgRoot, gradientId, regionGradientStops(subgroup))) {
        polygon.setStyle({ fillColor: `url(#${gradientId})` });
      }
    });
  });

  drawIncidentConnectors(incidentFragmentPoints);

  // Detections belonging to a polygon hidden by the sidebar filters (above)
  // shouldn't reappear as loose clustered/isolated dots either.
  const visiblePointFires = pointFires.filter((f) => !filteredOutPoints.has(f));

  if (lowZoom) {
    // The region polygons above are the primary visual at this zoom - only
    // isolated detections that didn't form a region (fewer than 3 nearby)
    // still get a small marker, same fixed radius as every other dot.
    visiblePointFires
      .filter((f) => !pointsInHullGroups.has(f))
      .forEach((fire) => {
        L.circleMarker([fire.latitude, fire.longitude], {
          radius: HOTSPOT_DOT_RADIUS,
          color: HOTSPOT_STROKE,
          weight: 1,
          fillColor: recencyColor(fire.acquired_at),
          fillOpacity: 0.85,
        })
          .bindPopup(`<div class="card-meta">Detectado &nbsp;${fire.acquired_at}</div>`)
          .addTo(markersLayer);
      });
  } else if (zoom >= INDIVIDUAL_DOT_ZOOM) {
    // Plot every raw detection as its own small fixed-radius dot - shows the
    // actual hotspot density/shape texture (matches how other fire-monitoring
    // maps render FIRMS/VIIRS data), not just a handful of blobs. Each dot
    // keeps its own recency color rather than a cluster-averaged one, so the
    // color gradient across dots reads as spread direction over time.
    visiblePointFires.forEach((fire) => {
      L.circleMarker([fire.latitude, fire.longitude], {
        radius: HOTSPOT_DOT_RADIUS,
        color: HOTSPOT_STROKE,
        weight: 1,
        fillColor: recencyColor(fire.acquired_at),
        fillOpacity: 0.9,
      })
        .bindPopup(`<div class="card-meta">Detectado &nbsp;${fire.acquired_at}</div>`)
        .addTo(markersLayer);
    });
  } else {
    // Between LOW_ZOOM_POLYGON_ONLY and INDIVIDUAL_DOT_ZOOM, bucket nearby
    // detections purely to cap the number of SVG dots drawn (lastFires isn't
    // viewport-filtered, so a region-wide view can hold thousands of points).
    // This is decimation for rendering performance only - every bucket still
    // renders at the SAME fixed radius as an individual dot, never bigger, so
    // dot size never encodes detection count. Color still comes from the
    // bucket's most recent detection, so the red-to-yellow gradient across
    // buckets still reads as spread direction, just at coarser granularity
    // than the fully zoomed-in per-point view.
    clusterPointFires(visiblePointFires, gridDeg).forEach((cluster) => {
      const fillColor = recencyColor(cluster.acquired_at);
      const popupHtml =
        `<div class="card-title">${cluster.count} detecci${cluster.count > 1 ? "ones" : "ón"} cercana${cluster.count > 1 ? "s" : ""}</div>` +
        `<div class="card-meta">Más reciente &nbsp;${cluster.acquired_at}</div>`;

      L.circleMarker([cluster.latitude, cluster.longitude], {
        radius: HOTSPOT_DOT_RADIUS,
        color: HOTSPOT_STROKE,
        weight: 1,
        fillColor,
        fillOpacity: 0.9,
      })
        .bindPopup(popupHtml)
        .addTo(markersLayer);
    });
  }

  lastReports.forEach((report) => {
    if (report.latitude == null || report.longitude == null) return;
    L.circleMarker([report.latitude, report.longitude], {
      radius: 6,
      color: REPORT_COLOR,
      fillColor: REPORT_COLOR,
      fillOpacity: 0.8,
    })
      .bindPopup(
        `<div class="card-title">Reporte de usuario</div>` +
          `<div class="card-meta">${report.hashtag_location ?? ""}<br/>${report.notes ?? ""}</div>`
      )
      .addTo(markersLayer);
  });

  setStatus(`${lastFires.length} detecciones de incendio, ${lastReports.length} reportes de usuarios`);
}

const RISK_LABELS = { low: "Bajo", moderate: "Moderado", high: "Alto", critical: "Crítico" };
const STATUS_LABELS = { active: "Activo", cooling: "En enfriamiento", archived: "Archivado" };

// Accessibility: risk badges shouldn't rely on color alone (color-blind
// users, low-contrast phone screens in bright sunlight) - each level also
// gets a distinct filled shape, escalating in number of sides
// (circle -> triangle -> diamond -> octagon) as a second, color-independent
// signal of severity.
const RISK_SHAPE_SVG = {
  low: '<svg viewBox="0 0 14 14" width="9" height="9" class="icon"><circle cx="7" cy="7" r="5" fill="currentColor"/></svg>',
  moderate: '<svg viewBox="0 0 14 14" width="9" height="9" class="icon"><path d="M7 2l5 9H2z" fill="currentColor"/></svg>',
  high: '<svg viewBox="0 0 14 14" width="9" height="9" class="icon"><path d="M7 1l6 6-6 6-6-6z" fill="currentColor"/></svg>',
  critical:
    '<svg viewBox="0 0 14 14" width="9" height="9" class="icon"><path d="M4.5 1h5L13 4.5v5L9.5 13h-5L1 9.5v-5z" fill="currentColor"/></svg>',
};

// risk_level is the fire's PEAK severity (backend's _severity() score never
// decays - see services/incidents.py) - it does NOT reflect whether the fire
// is still active. Without this, a long-cooling incident that was critical
// at its worst still shows a solid red "CRÍTICO" badge indefinitely, reading
// as an ongoing emergency rather than history. Muting the badge (dim +
// grayscale) once status isn't "active" keeps the information (this WAS a
// critical fire) without it competing visually with genuinely active ones.
function riskBadgeHtml(riskLevel, status) {
  const shape = RISK_SHAPE_SVG[riskLevel] || "";
  const inactiveClass = status && status !== "active" ? " risk-badge-inactive" : "";
  const title = status && status !== "active" ? ` title="Gravedad máxima alcanzada - el incidente ya no está activo"` : "";
  return `<span class="risk-badge risk-${riskLevel}${inactiveClass}"${title}>${shape} ${RISK_LABELS[riskLevel] || riskLevel}</span>`;
}

function relativeTime(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const hours = ms / 3600000;
  if (hours < 1) return `hace ${Math.max(1, Math.round(ms / 60000))} min`;
  if (hours < 48) return `hace ${Math.round(hours)} h`;
  return `hace ${Math.round(hours / 24)} d`;
}

// Same official-EFFIS-first, hull-estimate-fallback rule as the detail view
// (showIncidentDetail) - kept as its own helper since the list card and the
// detail card both need it. incidentEstimatesById is only populated by the
// most recent renderMap() pass, so a brand-new incident (or one outside the
// current map viewport/zoom) simply won't have a hectares figure yet here -
// that's a one-refresh lag, not a bug, and it self-corrects on the next pass.
function incidentAreaHa(incident) {
  const growth = incidentEstimatesById.get(incident.id) || null;
  if (incident.area_ha != null) return { areaHa: incident.area_ha, isOfficial: true };
  if (growth && growth.areaHa >= 0.1) return { areaHa: growth.areaHa, isOfficial: false };
  return null;
}

function incidentCardHtml(incident) {
  const name = incident.locality || `Foco sin nombre #${incident.id}`;
  const place = incident.province ? `${name} · ${incident.province}` : name;
  const area = incidentAreaHa(incident);
  const areaLine = area
    ? `${ICONS.flame} ${Math.round(area.areaHa).toLocaleString()} ha${area.isOfficial ? "" : " (estimado)"} · `
    : `${ICONS.flame} `;
  return (
    `<div class="incident-card-top">` +
    `<div class="incident-card-title">${place}</div>` +
    `${riskBadgeHtml(incident.risk_level, incident.status)}` +
    `</div>` +
    `<div class="incident-card-meta">` +
    `${areaLine}${incident.detection_count} detecci${incident.detection_count > 1 ? "ones" : "ón"} · ${STATUS_LABELS[incident.status] || incident.status}<br/>` +
    `${ICONS.clock} ${relativeTime(incident.last_detected_at)}` +
    `</div>`
  );
}

// Reads the sidebar's checkbox groups. An empty selection in a group means
// "no restriction from this group" (rather than "hide everything") - that
// keeps unchecking every box in the opt-in "Confirmed by" group behave the
// same as its unchecked-by-default starting state, and avoids risk/status
// groups trapping a user in an all-hidden state.
function checkedValues(selector) {
  return Array.from(document.querySelectorAll(selector))
    .filter((el) => el.checked)
    .map((el) => el.value);
}

// Accent/case-insensitive substring match - "almeria" should find "Almería".
function normalizeSearchText(text) {
  return (text || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

function getActiveFilters() {
  return {
    risks: checkedValues(".filter-risk"),
    statuses: checkedValues(".filter-status"),
    sourceKeys: checkedValues(".filter-source"),
    searchText: normalizeSearchText(document.getElementById("locality-search").value.trim()),
  };
}

// Checking multiple values within a group is OR ("any of low/high"); the
// three groups combine with AND ("high risk AND active AND has satellite imagery").
function incidentPassesFilters(incident, filters) {
  if (filters.risks.length && !filters.risks.includes(incident.risk_level)) return false;
  if (filters.statuses.length && !filters.statuses.includes(incident.status)) return false;
  if (filters.sourceKeys.length && !filters.sourceKeys.some((key) => incident[key])) return false;
  if (filters.searchText) {
    const haystack = normalizeSearchText(`${incident.locality || ""} ${incident.province || ""}`);
    if (!haystack.includes(filters.searchText)) return false;
  }
  return true;
}

function applyFilters(incidents) {
  const filters = getActiveFilters();
  return incidents.filter((incident) => incidentPassesFilters(incident, filters));
}

// Re-derives the visible sidebar list from lastIncidents + the current
// filter checkboxes, without any new backend round-trip - and re-renders the
// map so matching polygons respect the same filters (stretch goal; safe
// because it only ever skips drawing a group, it doesn't touch the
// clustering/hull math itself).
function refreshIncidentList() {
  renderIncidentList(applyFilters(lastIncidents));
  renderMap();
}

// This app is Spain-focused, but FIRMS' bounding box spills slightly over the
// border (Portugal, France, Algeria, Morocco), so a few incidents per view
// are never going to be Spanish. Grouping them separately - instead of
// mixing "Ain Defla, Algeria" in between Spanish provinces - makes the
// primary "fires in Spain" list scannable without foreign entries breaking
// its visual rhythm. An incident with no resolved country yet (brand new,
// still being geocoded) defaults into the Spain group rather than a
// confusing third bucket - it's usually right, and self-corrects once
// resolved on the next incident rebuild pass.
function isSpainIncident(incident) {
  return !incident.country_code || incident.country_code === "ES";
}

function incidentListSectionHtml(incidents) {
  if (incidents.length === 0) return "";
  return incidents
    .map((incident) => `<div class="incident-card" data-incident-id="${incident.id}">${incidentCardHtml(incident)}</div>`)
    .join("");
}

function renderIncidentList(incidents) {
  document.getElementById("sidebar-header").innerHTML =
    `<h2>Incendios por gravedad</h2><span class="count" id="incident-count"></span>`;
  document.getElementById("incident-count").textContent = `${incidents.length}`;

  const body = document.getElementById("sidebar-body");
  if (incidents.length === 0) {
    body.innerHTML = `<div class="sidebar-empty">No hay incidentes en esta ventana temporal.</div>`;
    return;
  }

  const spainIncidents = incidents.filter(isSpainIncident);
  const otherIncidents = incidents.filter((i) => !isSpainIncident(i));

  body.innerHTML =
    incidentListSectionHtml(spainIncidents) +
    (otherIncidents.length
      ? `<div class="incident-section-label">Otros países</div>` + incidentListSectionHtml(otherIncidents)
      : "");

  body.querySelectorAll(".incident-card").forEach((card) => {
    const incident = incidents.find((i) => i.id === Number(card.dataset.incidentId));
    card.addEventListener("click", () => showIncidentDetail(incident));
  });
}

// "26h" / "3d" style compact duration, for the incident card's big-number
// "tiempo activo" metric (first detection -> most recent one).
function durationLabel(startIso, endIso) {
  const hours = (new Date(endIso).getTime() - new Date(startIso).getTime()) / 3600000;
  if (hours < 1) return "<1h";
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

// Telegram events store {"media_path": "..."} - a filename served from our
// own /media mount. Copernicus events store {"scene_db_id": N} - the <img>
// tag's own GET to /api/copernicus/scenes/{id}/thumbnail is what triggers
// the (lazy, quota-costing) Process API render on first view; thumbnail_url
// is a leftover Catalog API field that's always null in practice (confirmed
// live - sentinel-2-l2a responses don't include a thumbnail asset).
function timelineEventImageUrl(event) {
  if (!event.raw_data) return null;
  try {
    const data = JSON.parse(event.raw_data);
    if (data.scene_db_id) return `${apiBaseUrl}/api/copernicus/scenes/${data.scene_db_id}/thumbnail`;
    if (data.thumbnail_url) return data.thumbnail_url;
    if (data.media_path) return `${apiBaseUrl}/media/${encodeURIComponent(data.media_path)}`;
  } catch {
    return null;
  }
  return null;
}

// One icon per event_type (services/incidents.py, copernicus.py,
// telegram.py, regional_incidents/sync.py) so the timeline is scannable by
// shape/color, not just by reading every line of text - matches the same
// "don't rely on a single visual channel" approach as the risk badges'
// shapes (RISK_SHAPE_SVG).
const EVENT_TYPE_ICON = {
  detection: ICONS.flame,
  status_change: ICONS.clock,
  telegram_message: ICONS.send,
  satellite_imagery: ICONS.camera,
  regional_status: ICONS.shield,
};

// Consecutive same-day "detection" events (see dailyDetectionCounts' regex
// note on why these titles are parseable) collapse into one summary line -
// a fire with several rebuild passes a day previously showed as 4-5 nearly
// identical "N detección(es) nueva(s)" rows in a row, drowning out the
// milestone events (status changes, imagery, mentions) around them. The
// very first event ("Primera detección") stays its own line always, since
// it's the one detection event that's actually a distinct milestone.
function groupTimelineEvents(events) {
  const grouped = [];
  events.forEach((event, i) => {
    const isFirstDetection = i === 0 && event.event_type === "detection";
    const day = (event.occurred_at || "").slice(0, 10);
    const prev = grouped[grouped.length - 1];
    if (
      !isFirstDetection &&
      event.event_type === "detection" &&
      prev &&
      prev.event_type === "detection" &&
      !prev.isFirstDetection &&
      prev.day === day
    ) {
      prev.detectionSum += detectionEventCount(event);
      prev.title = `${prev.detectionSum} detección(es) nueva(s)`;
      prev.occurred_at = event.occurred_at; // keep the latest timestamp in the merged range
      return;
    }
    grouped.push({
      ...event,
      day,
      isFirstDetection,
      detectionSum: event.event_type === "detection" ? detectionEventCount(event) : 0,
    });
  });
  return grouped;
}

function timelineItemHtml(event) {
  // Satellite scenes get their own carousel above the timeline (see
  // satelliteCarouselHtml) - showing the same full-size image again inline
  // here would just duplicate it and add scroll length for no new signal.
  const imageUrl = event.event_type === "satellite_imagery" ? null : timelineEventImageUrl(event);
  const icon = EVENT_TYPE_ICON[event.event_type] || "";
  return (
    `<div class="timeline-item">` +
    `<span class="timeline-dot timeline-dot-${event.event_type || "default"}">${icon}</span>` +
    `<div class="timeline-time">${event.occurred_at}</div>` +
    `<div class="timeline-title">${event.title}</div>` +
    (event.description ? `<div class="timeline-desc">${event.description}</div>` : "") +
    (imageUrl ? `<img src="${imageUrl}" class="timeline-thumb" />` : "") +
    `</div>`
  );
}

// Horizontal, swipeable filmstrip of every Copernicus scene for this
// incident, in chronological order - a direct visual "how did the burn scar
// change over time" view, instead of scrolling through a mixed event list
// where images are interleaved with unrelated detection/status/Telegram rows.
function satelliteCarouselHtml(events) {
  const scenes = events.filter((e) => e.event_type === "satellite_imagery");
  if (scenes.length === 0) return "";

  const slides = scenes
    .map((event) => {
      const imageUrl = timelineEventImageUrl(event);
      if (!imageUrl) return "";
      const cloudMatch = /\((\d+)% nubes\)/.exec(event.title || "");
      const dateLabel = new Date(event.occurred_at).toLocaleDateString("es-ES", { day: "numeric", month: "short" });
      return (
        `<div class="satellite-slide">` +
        `<img src="${imageUrl}" loading="lazy" />` +
        `<div class="satellite-slide-caption">${dateLabel}${cloudMatch ? ` · ${cloudMatch[1]}% nubes` : ""}</div>` +
        `</div>`
      );
    })
    .join("");

  return (
    `<div class="satellite-carousel-wrap">` +
    `<div class="satellite-carousel-label">${ICONS.camera} Evolución vía satélite (${scenes.length})</div>` +
    `<div class="satellite-carousel">${slides}</div>` +
    `</div>`
  );
}

async function showIncidentDetail(incident) {
  map.flyTo([incident.centroid_lat, incident.centroid_lon], Math.max(map.getZoom(), 11));

  // Filters apply to the list, not to a single incident's detail - collapse
  // them to give the detail card the space instead of leaving them expanded
  // above it for no reason. Restored (below) when going back to the list.
  document.getElementById("filter-panel").classList.add("collapsed");

  document.getElementById("sidebar-header").innerHTML =
    `<button class="sidebar-back" id="sidebar-back" title="Volver a la lista">&larr;</button><h2>Detalle del incidente</h2>`;
  document.getElementById("sidebar-back").addEventListener("click", () => {
    document.getElementById("filter-panel").classList.remove("collapsed");
    refreshIncidentList();
  });

  const name = incident.locality || `Foco sin nombre #${incident.id}`;
  const body = document.getElementById("sidebar-body");

  // Prefer the official EFFIS area (more accurate) over our own hull-based
  // estimate; the estimate (and its growth trend) only exists if this
  // incident's polygon was drawn in the last renderMap() pass - it may be
  // missing for an incident outside the currently-loaded detection set.
  const growth = incidentEstimatesById.get(incident.id) || null;
  const hasOfficialArea = incident.area_ha != null;
  const areaHa = hasOfficialArea ? incident.area_ha : growth && growth.areaHa >= 0.1 ? growth.areaHa : null;

  // Big-number metrics first (what a stressed-out user needs at a glance),
  // event history moved to a secondary, collapsed-by-default disclosure -
  // it's supporting detail, not the primary read of "how bad is this fire".
  body.innerHTML =
    `<div class="incident-detail-meta">` +
    `<div class="incident-detail-title">${name}</div>` +
    (incident.province ? `<div class="incident-detail-sub">${incident.province}</div>` : "") +
    `<div class="incident-detail-badges">` +
    `${riskBadgeHtml(incident.risk_level, incident.status)}` +
    `<span class="risk-badge" style="background:var(--bg-elevated); color:var(--text-secondary);">${STATUS_LABELS[incident.status] || incident.status}</span>` +
    (growth ? growthBadgeHtml(growth) : "") +
    `</div>` +
    `<div class="incident-detail-metrics">` +
    `<div class="incident-metric"><div class="incident-metric-value">${incident.detection_count}</div><div class="incident-metric-label">Detecciones</div></div>` +
    `<div class="incident-metric"><div class="incident-metric-value">${durationLabel(incident.first_detected_at, incident.last_detected_at)}</div><div class="incident-metric-label">Tiempo activo</div></div>` +
    (areaHa != null
      ? `<div class="incident-metric"><div class="incident-metric-value">${Math.round(areaHa).toLocaleString()}</div><div class="incident-metric-label">Hectáreas${hasOfficialArea ? "" : " (estimado)"}</div></div>`
      : `<div class="incident-metric"><div class="incident-metric-value" style="font-size:15px;">${relativeTime(incident.last_detected_at)}</div><div class="incident-metric-label">Última actualización</div></div>`) +
    `</div>` +
    // Placeholder - filled in once the full-history timeline loads below.
    // The old version rendered this synchronously from `growth.timestamps`
    // (only whatever's currently loaded on the map under the active
    // date-range filter), which is why a 10-day-old incident's chart looked
    // almost empty when the map was showing "last 48h" - this now always
    // reflects the incident's REAL full history regardless of that filter.
    `<div id="daily-chart-slot"></div>` +
    `</div>` +
    `<div id="satellite-carousel-slot"></div>` +
    `<button class="timeline-toggle" id="timeline-toggle">` +
    `<span>Ver cronología</span><span class="timeline-toggle-chevron">▾</span>` +
    `</button>` +
    `<div class="timeline-list" id="timeline-list"><div class="sidebar-empty">Cargando cronología…</div></div>`;

  document.getElementById("timeline-toggle").addEventListener("click", () => {
    document.getElementById("timeline-toggle").classList.toggle("expanded");
    document.getElementById("timeline-list").classList.toggle("expanded");
  });

  try {
    // Deliberately NOT filtered by the map/sidebar's date-range selector
    // (getSelectedDays) - a specific incident's own timeline should always
    // show its full history (first detection -> stabilization -> any later
    // reactivation), regardless of which window you're currently browsing
    // the map at. The date-range filter still scopes what shows up on the
    // map/sidebar list itself, just not a single incident's own detail.
    const res = await fetch(`${apiBaseUrl}/api/incidents/${incident.id}/timeline`);
    const events = await res.json();

    document.getElementById("daily-chart-slot").innerHTML = dailyActivityChartHtml(events);
    document.getElementById("satellite-carousel-slot").innerHTML = satelliteCarouselHtml(events);

    const list = document.getElementById("timeline-list");
    list.innerHTML = events.length
      ? groupTimelineEvents(events).map(timelineItemHtml).join("")
      : `<div class="sidebar-empty">Sin eventos registrados para este incidente.</div>`;
  } catch (err) {
    document.getElementById("timeline-list").innerHTML =
      `<div class="sidebar-empty">No se pudo cargar la cronología.</div>`;
  }
}

let lastIncidents = [];

// Matches a map polygon's centroid to a backend FireIncident so the popup can
// show Telegram mentions and open the same timeline the sidebar uses - the
// polygon here is grouped client-side (groupFiresByProximity) independently
// from the backend's own clustering (services/incidents.py), but both use the
// same REGION_LINK_DEG threshold, so their centroids should coincide closely.
function findMatchingIncident(lat, lon) {
  let best = null;
  let bestDist = INCIDENT_REASSOCIATION_DEG;
  lastIncidents.forEach((incident) => {
    const dist = Math.hypot(incident.centroid_lat - lat, incident.centroid_lon - lon);
    if (dist <= bestDist) {
      bestDist = dist;
      best = incident;
    }
  });
  return best;
}

// The header dot next to the app name only pulses when there's something
// actually happening right now - a permanently-animating indicator trains
// users to ignore it, which is the opposite of what it's for in an emergency
// monitoring app.
function updateHeaderPulse(incidents) {
  const hasActive = incidents.some((incident) => incident.status === "active");
  document.querySelectorAll("#topbar-pulse, #header-pulse").forEach((el) => {
    el.classList.toggle("is-active", hasActive);
  });
}

async function loadIncidents() {
  try {
    // Same hours window as the map's date-range filter (getSelectedDays), so
    // the sidebar list, the map, and any open timeline all agree on "what's
    // in scope right now" instead of the sidebar always showing 30 days.
    const hours = getSelectedDays() * 24;
    const res = await fetch(`${apiBaseUrl}/api/incidents?sort=severity&hours=${hours}`);
    lastIncidents = await res.json();
    renderIncidentList(applyFilters(lastIncidents));
    updateHeaderPulse(lastIncidents);
    notifyNewCriticalIncidents(lastIncidents);
  } catch (err) {
    document.getElementById("sidebar-body").innerHTML =
      `<div class="sidebar-empty">No se pudieron cargar los incidentes.</div>`;
  }
}

async function loadFires() {
  try {
    const hours = getSelectedDays() * 24;
    const [firesRes, reportsRes] = await Promise.all([
      fetch(`${apiBaseUrl}/api/fires?hours=${hours}`),
      fetch(`${apiBaseUrl}/api/reports`),
    ]);
    lastFires = await firesRes.json();
    lastReports = await reportsRes.json();
    // Awaited before the first render so map polygons can be matched to a
    // backend incident (findMatchingIncident) as soon as they're drawn -
    // otherwise the first paint would have no incidents loaded yet to match against.
    await loadIncidents();
    renderMap();
  } catch (err) {
    setStatus(`No se pudieron cargar los datos: ${err.message}`);
  }
}

// ---------- Webcams (Windy-style: pins on the map, click for a live
// snapshot + nearby-cameras strip to browse without closing the popup) ----------

// Cache of "nearby" results per camera id, so re-clicking the same pin (or
// clicking a thumbnail back to one already seen) doesn't re-fetch.
const webcamNearbyCache = new Map();

async function fetchWebcamsInView() {
  const bounds = map.getBounds();
  const bbox = [
    bounds.getWest(),
    bounds.getSouth(),
    bounds.getEast(),
    bounds.getNorth(),
  ].join(",");
  // DGT (pre-synced, our own DB) and Windy (fetched live every time - its
  // image URLs carry a short-lived token, so it's never cached/persisted)
  // are two independent sources merged into one layer. If Windy's request
  // fails (missing/invalid key, quota, etc.) DGT cameras still show - one
  // source's outage shouldn't blank the whole webcams layer.
  const fetchJsonArray = async (url) => {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  };
  const [dgtResult, windyResult] = await Promise.allSettled([
    fetchJsonArray(`${apiBaseUrl}/api/webcams?bbox=${bbox}&limit=500`),
    fetchJsonArray(`${apiBaseUrl}/api/webcams/windy?bbox=${bbox}&limit=50`),
  ]);
  const dgt = dgtResult.status === "fulfilled" ? dgtResult.value : [];
  const windy = windyResult.status === "fulfilled" ? windyResult.value : [];
  return [...dgt, ...windy];
}

async function getNearbyWebcams(webcam) {
  if (webcamNearbyCache.has(webcam.id)) return webcamNearbyCache.get(webcam.id);
  const res = await fetch(
    `${apiBaseUrl}/api/webcams/nearby?lat=${webcam.latitude}&lon=${webcam.longitude}&exclude_id=${webcam.id}&limit=6`
  );
  const data = await res.json();
  webcamNearbyCache.set(webcam.id, data);
  return data;
}

function webcamPopupHtml(webcam, nearby) {
  const place = [webcam.road, webcam.province].filter(Boolean).join(" · ");
  const nearbyStrip = (nearby || [])
    .map(
      (w) =>
        `<img src="${w.image_url}" class="webcam-nearby-thumb" data-webcam-id="${w.id}" title="${w.name || ""}" />`
    )
    .join("");
  return (
    `<div class="card-title">${ICONS.camera} ${webcam.name || "Cámara de tráfico"}</div>` +
    (place ? `<div class="card-meta">${place}</div>` : "") +
    `<img src="${webcam.image_url}?t=${Date.now()}" class="webcam-thumb" />` +
    `<div class="card-caveat">Imagen de ${webcam.source === "windy" ? "Windy" : "la DGT"} - se actualiza cada vez que abres este popup</div>` +
    (nearbyStrip
      ? `<div class="webcam-nearby-label">Cámaras cercanas</div><div class="webcam-nearby-strip">${nearbyStrip}</div>`
      : "")
  );
}

// A marker's popup lazily loads its "nearby" list once opened, and clicking
// a nearby thumbnail re-centers the map and opens THAT camera's popup in
// place - the same "keep browsing without losing your spot" pattern Windy's
// webcam viewer uses.
function bindWebcamPopup(marker, webcam) {
  const render = (nearby) => webcamPopupHtml(webcam, nearby);
  marker.bindPopup(render(webcamNearbyCache.get(webcam.id) || null));
  marker.on("popupopen", async (e) => {
    const container = e.popup.getElement();
    container.querySelectorAll(".webcam-nearby-thumb").forEach((thumb) => {
      thumb.addEventListener("click", () => {
        const targetId = Number(thumb.dataset.webcamId);
        const targetMarker = webcamMarkersById.get(targetId);
        if (!targetMarker) return;
        map.closePopup();
        map.panTo(targetMarker.getLatLng());
        targetMarker.openPopup();
      });
    });
    if (!webcamNearbyCache.has(webcam.id)) {
      try {
        const nearby = await getNearbyWebcams(webcam);
        marker.setPopupContent(render(nearby));
      } catch {
        // popup still shows the main image even if the nearby strip fails
      }
    }
  });
}

const webcamMarkersById = new Map();

async function reloadWebcams() {
  if (!document.getElementById("webcams-toggle").checked) return;
  try {
    const webcams = await fetchWebcamsInView();
    webcamsLayer.clearLayers();
    webcamMarkersById.clear();
    webcams.forEach((webcam) => {
      const marker = L.circleMarker([webcam.latitude, webcam.longitude], {
        radius: 6,
        color: "#1e293b",
        weight: 1.5,
        fillColor: "#38bdf8",
        fillOpacity: 0.9,
      });
      bindWebcamPopup(marker, webcam);
      marker.addTo(webcamsLayer);
      webcamMarkersById.set(webcam.id, marker);
    });
  } catch (err) {
    setStatus(`No se pudieron cargar las cámaras: ${err.message}`);
  }
}

function toggleWebcamsLayer() {
  const enabled = document.getElementById("webcams-toggle").checked;
  if (enabled) {
    webcamsLayer.addTo(map);
    reloadWebcams();
  } else {
    map.removeLayer(webcamsLayer);
  }
}

// ---------- Fire spread prediction (experimental POC) ----------
// Click "Place origin", then click the map: fetches /api/fire-spread/predict
// (an hourly series, up to 24h, driven by the Open-Meteo forecast - not a
// single static wind reading) and draws one ellipse per hour, scrubbed via a
// Windy-style time slider (see app/services/fire_spread.py for the model and
// its real caveats).
const fireSpreadLayer = L.layerGroup().addTo(map);
const fireSpreadOriginLayer = L.layerGroup().addTo(map);
let placingFireOrigin = false;
let fireSpreadData = null; // last prediction response, kept so the slider can redraw without refetching
let fireSpreadOrigin = null;

const COMPASS_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
function compassLabel(degrees) {
  return COMPASS_POINTS[Math.round(degrees / 22.5) % 16];
}

function fireSpreadHourHtml(data, hourEntry) {
  const fuel = data.fuel;
  const slope = data.slope;
  const ros = hourEntry.rate_of_spread_m_per_min;
  const distanceKm = (hourEntry.cumulative_head_m / 1000).toFixed(2);
  return (
    `<div class="fire-spread-card">` +
    `<div class="fire-spread-row"><b>+${hourEntry.hour}h</b> (${hourEntry.time.replace("T", " ")} UTC)</div>` +
    `<div class="fire-spread-row"><b>Viento</b> ${hourEntry.wind_speed_kmh} km/h desde el ${compassLabel(hourEntry.wind_direction_from_deg)}</div>` +
    `<div class="fire-spread-row"><b>Combustible</b> ${fuel.label}${fuel.clc_code ? ` (CLC ${fuel.clc_code})` : ""}</div>` +
    `<div class="fire-spread-row"><b>Pendiente</b> ${
      slope.unavailable
        ? "no disponible (se asume terreno llano) - falló la consulta de elevación"
        : `${slope.slope_degrees.toFixed(1)}° ${slope.slope_degrees > 0 ? "cuesta arriba" : "cuesta abajo"} (dirección inicial de propagación)`
    }</div>` +
    `<div class="fire-spread-row"><b>Velocidad de propagación</b> frente ${ros.head} · flancos ${ros.flank} · cola ${ros.back} m/min</div>` +
    `<div class="fire-spread-row"><b>Distancia máxima alcanzada</b> ~${distanceKm} km</div>` +
    (hourEntry.head_blocked_by_water
      ? `<div class="fire-spread-row" style="color:var(--accent);">${ICONS.droplet} El frente se detiene en una masa de agua (río, embalse o costa)</div>`
      : "") +
    `<div class="card-caveat">${data.disclaimer}</div>` +
    `</div>`
  );
}

function renderFireSpreadHour(hourIndex) {
  if (!fireSpreadData) return;
  const hourEntry = fireSpreadData.hourly[hourIndex];
  if (!hourEntry) return;

  fireSpreadLayer.clearLayers();
  L.polygon(hourEntry.polygon, {
    color: "#f87171",
    weight: 2,
    fillColor: "#f87171",
    fillOpacity: 0.22,
  })
    .bindTooltip(`+${hourEntry.hour}h · up to ~${Math.round(hourEntry.cumulative_head_m)}m`, { sticky: true })
    .addTo(fireSpreadLayer);

  document.getElementById("fire-spread-info").innerHTML = fireSpreadHourHtml(fireSpreadData, hourEntry);
  document.getElementById("fire-spread-hour-label").textContent = `+${hourEntry.hour}h`;
}

async function predictFireSpread(lat, lon) {
  const infoEl = document.getElementById("fire-spread-info");
  infoEl.innerHTML = `<div class="sidebar-empty" style="padding:8px 0;">Calculando…</div>`;
  fireSpreadLayer.clearLayers();
  fireSpreadOriginLayer.clearLayers();
  fireSpreadData = null;
  fireSpreadOrigin = [lat, lon];
  document.getElementById("fire-spread-slider-row").style.display = "none";

  L.circleMarker([lat, lon], {
    radius: 6,
    color: "#000",
    weight: 1.5,
    fillColor: "#ff6a3d",
    fillOpacity: 1,
  })
    .bindTooltip("Origen del incendio (colocado)", { permanent: false })
    .addTo(fireSpreadOriginLayer);

  try {
    const res = await fetch(`${apiBaseUrl}/api/fire-spread/predict?lat=${lat}&lon=${lon}&max_hours=24`);
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    fireSpreadData = data;

    const slider = document.getElementById("fire-spread-slider");
    slider.min = 0;
    slider.max = data.hourly.length - 1;
    slider.value = 0;
    document.getElementById("fire-spread-slider-row").style.display = "";

    renderFireSpreadHour(0);
  } catch (err) {
    infoEl.innerHTML = `<div class="sidebar-empty" style="padding:8px 0;">La predicción falló: ${err.message}</div>`;
  }
}

function clearFireSpread() {
  fireSpreadLayer.clearLayers();
  fireSpreadOriginLayer.clearLayers();
  fireSpreadData = null;
  fireSpreadOrigin = null;
  document.getElementById("fire-spread-info").innerHTML = "";
  document.getElementById("fire-spread-slider-row").style.display = "none";
  placingFireOrigin = false;
  document.getElementById("fire-spread-place").classList.remove("active-placing");
  map.getContainer().style.cursor = "";
}

// Triggering external ingestion (FIRMS/EFFIS/admin bulletins/Telegram) now
// lives on the status page (/status.html) next to each source's health
// history, not here - "Reload map" only re-reads what's already in our own
// DB via /api/fires and /api/incidents.
document.getElementById("reload").addEventListener("click", loadFires);
document.getElementById("date-range").addEventListener("change", loadFires);
document.getElementById("satellite-toggle").addEventListener("change", updateSatelliteLayer);
document.getElementById("satellite-date").addEventListener("change", updateSatelliteLayer);
document.getElementById("webcams-toggle").addEventListener("change", toggleWebcamsLayer);
document.getElementById("basemap-style").addEventListener("change", (e) => setBasemapStyle(e.target.value));
document.getElementById("fire-spread-place").addEventListener("click", () => {
  placingFireOrigin = true;
  document.getElementById("fire-spread-place").classList.add("active-placing");
  map.getContainer().style.cursor = "crosshair";
});
document.getElementById("fire-spread-clear").addEventListener("click", clearFireSpread);
document.getElementById("fire-spread-tool-toggle").addEventListener("click", () => {
  document.getElementById("fire-spread-tool").classList.toggle("collapsed");
});
document.getElementById("fire-spread-slider").addEventListener("input", (e) => {
  renderFireSpreadHour(parseInt(e.target.value, 10));
});
map.on("click", (e) => {
  if (!placingFireOrigin) return;
  placingFireOrigin = false;
  document.getElementById("fire-spread-place").classList.remove("active-placing");
  map.getContainer().style.cursor = "";
  predictFireSpread(e.latlng.lat, e.latlng.lng);
});
// Re-cluster (finer grid when zoomed in) without re-fetching from the API.
map.on("zoomend", renderMap);
// Webcams are loaded per-viewport (a fixed bbox query, not all 1900+ cameras
// nationwide at once) - refetch whenever the visible area actually changes,
// but only while the layer is turned on.
map.on("moveend", reloadWebcams);

// Sidebar filters are pure client-side re-derivations of lastIncidents - no
// extra API round-trip on every checkbox click.
document.querySelectorAll(".filter-risk, .filter-status, .filter-source").forEach((checkbox) => {
  checkbox.addEventListener("change", refreshIncidentList);
});
document.getElementById("filter-reset").addEventListener("click", () => {
  document.querySelectorAll(".filter-risk, .filter-status").forEach((el) => (el.checked = true));
  document.querySelectorAll(".filter-source").forEach((el) => (el.checked = false));
  document.getElementById("locality-search").value = "";
  document.getElementById("quick-filter-critical").classList.remove("active");
  refreshIncidentList();
});
document.getElementById("filter-panel-toggle").addEventListener("click", () => {
  document.getElementById("filter-panel").classList.toggle("collapsed");
});

// ---------- Mobile: floating panels become full-screen overlays, opened one
// at a time via the topbar's toggle buttons instead of both being crammed
// onto a phone-width viewport at once. ----------
function openMobilePanel(panelEl) {
  document.getElementById("incident-sidebar").classList.remove("mobile-open");
  document.getElementById("panel").classList.remove("mobile-open");
  panelEl.classList.add("mobile-open");
}
function closeMobilePanels() {
  document.getElementById("incident-sidebar").classList.remove("mobile-open");
  document.getElementById("panel").classList.remove("mobile-open");
}
document.getElementById("mobile-sidebar-toggle").addEventListener("click", () =>
  openMobilePanel(document.getElementById("incident-sidebar"))
);
document.getElementById("mobile-settings-toggle").addEventListener("click", () =>
  openMobilePanel(document.getElementById("panel"))
);
document.getElementById("mobile-sidebar-close").addEventListener("click", closeMobilePanels);
document.getElementById("mobile-settings-close").addEventListener("click", closeMobilePanels);

document.getElementById("locality-search").addEventListener("input", refreshIncidentList);

// One-tap "only what matters under stress" - checks critical risk + active
// status only. Clicking again while active restores every risk/status
// checkbox instead of leaving the user stuck narrowed down with no visible
// way back (the existing "Restablecer filtros" link is easy to miss).
document.getElementById("quick-filter-critical").addEventListener("click", (e) => {
  const btn = e.currentTarget;
  const turningOn = !btn.classList.contains("active");
  btn.classList.toggle("active", turningOn);
  if (turningOn) {
    document.querySelectorAll(".filter-risk").forEach((el) => (el.checked = el.value === "critical"));
    document.querySelectorAll(".filter-status").forEach((el) => (el.checked = el.value === "active"));
  } else {
    document.querySelectorAll(".filter-risk, .filter-status").forEach((el) => (el.checked = true));
  }
  refreshIncidentList();
});

// Click-to-zoom for any thumbnail (timeline scenes, Telegram photos,
// satellite previews) - event-delegated on document since these images are
// inserted dynamically via innerHTML long after page load, so a listener
// bound directly to each <img> at creation time would miss ones added later.
//
// `images` is an array of {src, caption} - when it has more than one entry
// (the satellite carousel's own scenes, in order - see the click handler
// below), prev/next arrows and arrow-key navigation let you step through the
// whole sequence without closing and reopening the lightbox each time, which
// is the point of viewing them "in the middle, zoomed in" in the first
// place rather than the small inline filmstrip.
function openLightbox(images, startIndex) {
  let index = startIndex || 0;
  const overlay = document.createElement("div");
  overlay.id = "lightbox-overlay";

  function render() {
    const { src, caption } = images[index];
    const showNav = images.length > 1;
    overlay.innerHTML =
      `<button class="lightbox-close" aria-label="Cerrar">&times;</button>` +
      (showNav ? `<button class="lightbox-nav lightbox-prev" aria-label="Anterior">&#8249;</button>` : "") +
      `<div class="lightbox-image-wrap"><img src="${src}" />` +
      (caption ? `<div class="lightbox-caption">${caption}${showNav ? ` · ${index + 1}/${images.length}` : ""}</div>` : "") +
      `</div>` +
      (showNav ? `<button class="lightbox-nav lightbox-next" aria-label="Siguiente">&#8250;</button>` : "");
  }
  render();

  function go(delta) {
    index = (index + delta + images.length) % images.length;
    render();
  }

  overlay.addEventListener("click", (e) => {
    if (e.target.closest(".lightbox-nav")) {
      go(e.target.closest(".lightbox-prev") ? -1 : 1);
      return;
    }
    if (e.target.closest(".lightbox-image-wrap")) return; // clicking the image/caption itself shouldn't close it
    overlay.remove();
    document.removeEventListener("keydown", onKeydown);
  });
  function onKeydown(e) {
    if (e.key === "Escape") {
      overlay.remove();
      document.removeEventListener("keydown", onKeydown);
    } else if (e.key === "ArrowLeft" && images.length > 1) {
      go(-1);
    } else if (e.key === "ArrowRight" && images.length > 1) {
      go(1);
    }
  }
  document.addEventListener("keydown", onKeydown);
  document.body.appendChild(overlay);
}

// Capture phase (not bubble): Leaflet calls stopPropagation() on clicks
// inside popups (via L.DomEvent.disableClickPropagation) to stop them
// reaching the map - that only blocks further bubbling upward, not capturing
// listeners on the way down, so this still fires for popup thumbnails too.
document.addEventListener(
  "click",
  (e) => {
    const satelliteSlideImg = e.target.closest(".satellite-slide img");
    if (satelliteSlideImg) {
      // Gallery = every scene in THIS incident's carousel (not the whole
      // page), positioned at the one actually clicked.
      const slides = Array.from(satelliteSlideImg.closest(".satellite-carousel").querySelectorAll(".satellite-slide"));
      const images = slides.map((slide) => ({
        src: slide.querySelector("img").src,
        caption: slide.querySelector(".satellite-slide-caption").textContent,
      }));
      openLightbox(images, slides.indexOf(satelliteSlideImg.closest(".satellite-slide")));
      return;
    }
    const img = e.target.closest(".timeline-thumb, .telegram-thumb, .satellite-thumb, .webcam-thumb");
    if (img) openLightbox([{ src: img.src }], 0);
  },
  true
);

// Shared hover tooltip for the daily activity chart's bars - a real
// positioned tooltip (built once, reused) reads far better than the native
// SVG <title> hover-delay/tiny-font look the first version used, and (unlike
// <title>) also works via touch on mobile through the same delegated
// listener (see the "click" fallback below).
const dailyChartTooltip = document.createElement("div");
dailyChartTooltip.className = "daily-chart-tooltip";
document.body.appendChild(dailyChartTooltip);

function showDailyChartTooltip(bar, evt) {
  dailyChartTooltip.textContent = `${bar.dataset.label}: ${bar.dataset.count} detecci${bar.dataset.count === "1" ? "ón" : "ones"}`;
  dailyChartTooltip.style.left = `${evt.clientX}px`;
  dailyChartTooltip.style.top = `${evt.clientY - 10}px`;
  dailyChartTooltip.classList.add("visible");
}

document.addEventListener("mouseover", (e) => {
  const bar = e.target.closest(".daily-chart-bar");
  if (bar) showDailyChartTooltip(bar, e);
});
document.addEventListener("mousemove", (e) => {
  if (e.target.closest(".daily-chart-bar")) {
    dailyChartTooltip.style.left = `${e.clientX}px`;
    dailyChartTooltip.style.top = `${e.clientY - 10}px`;
  }
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest(".daily-chart-bar")) dailyChartTooltip.classList.remove("visible");
});
// Touch devices get no mouseover - tap shows the tooltip briefly instead.
document.addEventListener("touchstart", (e) => {
  const bar = e.target.closest(".daily-chart-bar");
  if (!bar) return;
  showDailyChartTooltip(bar, e.touches[0]);
  setTimeout(() => dailyChartTooltip.classList.remove("visible"), 1500);
});

// ---------- Keyboard shortcuts (for repeat users who keep this open for
// hours - firefighters/analysts, not just casual visitors) ----------
function focusedIncidentCards() {
  return Array.from(document.querySelectorAll("#sidebar-body .incident-card"));
}

function moveIncidentFocus(delta) {
  const cards = focusedIncidentCards();
  if (!cards.length) return;
  const currentIndex = cards.findIndex((c) => c.classList.contains("keyboard-focused"));
  const nextIndex = Math.max(0, Math.min(cards.length - 1, currentIndex + delta));
  cards.forEach((c) => c.classList.remove("keyboard-focused"));
  cards[nextIndex].classList.add("keyboard-focused");
  cards[nextIndex].scrollIntoView({ block: "nearest" });
}

document.addEventListener("keydown", (e) => {
  const tag = document.activeElement.tagName;
  const isTyping = tag === "INPUT" || tag === "TEXTAREA";

  if (e.key === "/" && !isTyping) {
    e.preventDefault();
    document.getElementById("locality-search").focus();
    return;
  }
  if (e.key === "Escape") {
    if (isTyping) document.activeElement.blur();
    map.closePopup();
    closeMobilePanels();
    return;
  }
  // Arrow navigation only makes sense over the incident list, not while
  // typing in the search box or viewing a single incident's detail.
  if (isTyping || document.getElementById("sidebar-back")) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    moveIncidentFocus(1);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    moveIncidentFocus(-1);
  } else if (e.key === "Enter") {
    const focused = document.querySelector("#sidebar-body .incident-card.keyboard-focused");
    if (focused) focused.click();
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

(function initSatelliteDate() {
  // Default to 2 days ago - GIBS "best available" imagery often lags a day or two.
  const d = new Date();
  d.setDate(d.getDate() - 2);
  document.getElementById("satellite-date").value = d.toISOString().slice(0, 10);
})();

// ---------- Location alerts (experimental POC, Phase 1) ----------
// Deliberately an in-app alert (browser Geolocation + Notification APIs,
// only fires while this tab is open), not a real push-notification backend -
// that would need a service worker, VAPID keys, a stored per-user
// subscription, and a background job on the server matching every
// subscription against active predictions. See README's "Proximity alerts"
// section for the two-phase reasoning behind this scope choice.
const LOCATION_ALERTS_STORAGE_KEY = "wm-location-alerts-enabled";
const PROXIMITY_CHECK_INTERVAL_MS = 5 * 60 * 1000;

let locationAlertsWatchId = null;
let proximityCheckTimer = null;
let lastKnownPosition = null;
// Only notify for a given incident once per page session - otherwise every
// 5-minute poll would re-fire the same alert as long as the condition holds.
const notifiedProximityIncidentIds = new Set();
const notifiedCriticalIncidentIds = new Set();
// True only after alerts are actually turned on - guards notifyNewCriticalIncidents
// (called on every incident list refresh regardless of whether alerts are
// enabled) from treating every pre-existing critical incident as "new" the
// instant the feature is switched on.
let locationAlertsActive = false;

function showBrowserNotification(title, body) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    new Notification(title, { body });
  } catch {
    // Some mobile browsers (Android Chrome in particular) require a Service
    // Worker registration to construct a Notification directly and throw
    // otherwise - this is a secondary nice-to-have, not core functionality,
    // so fail quietly rather than surface a confusing error to the user.
  }
}

function setLocationAlertsStatus(text) {
  const el = document.getElementById("location-alerts-status");
  if (el) el.textContent = text;
}

async function runProximityCheck() {
  if (!lastKnownPosition) return;
  const { latitude, longitude } = lastKnownPosition.coords;
  try {
    const res = await fetch(`${apiBaseUrl}/api/proximity/check?lat=${latitude}&lon=${longitude}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const alerts = await res.json();
    const checkedAt = new Date().toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
    setLocationAlertsStatus(
      alerts.length === 0
        ? `Sin incendios activos cerca de tu ubicación (comprobado a las ${checkedAt}).`
        : `⚠ ${alerts.length} incendio${alerts.length > 1 ? "s" : ""} activo${alerts.length > 1 ? "s" : ""} podría${alerts.length > 1 ? "n" : ""} alcanzar tu ubicación.`
    );
    alerts.forEach((alert) => {
      if (notifiedProximityIncidentIds.has(alert.incident_id)) return;
      notifiedProximityIncidentIds.add(alert.incident_id);
      const place = [alert.locality, alert.province].filter(Boolean).join(", ") || "un incendio cercano";
      showBrowserNotification(
        "⚠️ Alerta de incendio cercano",
        `La propagación prevista de ${place} podría alcanzar tu ubicación en ~${alert.hours_until_reach}h.`
      );
    });
  } catch (err) {
    setLocationAlertsStatus(`No se pudo comprobar la proximidad: ${err.message}`);
  }
}

// Called on every incident list refresh (loadIncidents), not just while
// alerts are enabled - the guard below just makes it a no-op otherwise so
// there's only one code path to keep in sync with "what counts as new".
function notifyNewCriticalIncidents(incidents) {
  const criticalActive = incidents.filter((i) => i.risk_level === "critical" && i.status === "active");
  if (!locationAlertsActive) {
    // Not enabled (yet) - just keep the "already seen" set current so that
    // turning alerts on later doesn't immediately notify for every critical
    // incident that already existed beforehand.
    criticalActive.forEach((i) => notifiedCriticalIncidentIds.add(i.id));
    return;
  }
  criticalActive.forEach((incident) => {
    if (notifiedCriticalIncidentIds.has(incident.id)) return;
    notifiedCriticalIncidentIds.add(incident.id);
    const name = incident.locality || `Foco sin nombre #${incident.id}`;
    showBrowserNotification(
      "🔥 Nuevo incendio crítico",
      `${name}${incident.province ? " · " + incident.province : ""}`
    );
  });
}

function enableLocationAlerts() {
  if (typeof Notification === "undefined" || !("geolocation" in navigator)) {
    setLocationAlertsStatus("Tu navegador no soporta geolocalización o notificaciones.");
    document.getElementById("location-alerts-toggle").checked = false;
    return;
  }
  Notification.requestPermission().then((permission) => {
    if (permission !== "granted") {
      setLocationAlertsStatus("Permiso de notificaciones denegado - actívalo en los ajustes del navegador para usar esta función.");
      document.getElementById("location-alerts-toggle").checked = false;
      return;
    }
    setLocationAlertsStatus("Buscando tu ubicación...");
    locationAlertsWatchId = navigator.geolocation.watchPosition(
      (position) => {
        lastKnownPosition = position;
        runProximityCheck();
      },
      (err) => setLocationAlertsStatus(`No se pudo obtener tu ubicación: ${err.message}`),
      { enableHighAccuracy: false, maximumAge: 5 * 60 * 1000, timeout: 20000 }
    );
    // Also re-fetches the incident list on the same cadence (not just
    // proximity) - loadIncidents() is what actually surfaces brand-new
    // critical incidents via notifyNewCriticalIncidents; without this,
    // "new incident" alerts would only ever fire after a manual reload or
    // date-range change.
    proximityCheckTimer = setInterval(() => {
      runProximityCheck();
      loadIncidents();
    }, PROXIMITY_CHECK_INTERVAL_MS);
    locationAlertsActive = true;
    localStorage.setItem(LOCATION_ALERTS_STORAGE_KEY, "1");
  });
}

function disableLocationAlerts() {
  if (locationAlertsWatchId !== null) navigator.geolocation.clearWatch(locationAlertsWatchId);
  if (proximityCheckTimer) clearInterval(proximityCheckTimer);
  locationAlertsWatchId = null;
  proximityCheckTimer = null;
  locationAlertsActive = false;
  localStorage.removeItem(LOCATION_ALERTS_STORAGE_KEY);
  setLocationAlertsStatus("");
}

document.getElementById("location-alerts-toggle").addEventListener("change", (e) => {
  if (e.target.checked) enableLocationAlerts();
  else disableLocationAlerts();
});

// If FIRMS itself hasn't successfully refreshed in a long while (scheduler
// down, upstream outage - visible in detail on /sources.html), the map can
// look deceptively "current" when it's actually showing stale detections.
// A quiet warning here means a user doesn't have to go check the status
// page to notice their view might be outdated.
const STALE_DATA_THRESHOLD_HOURS = 6;

async function checkStaleData() {
  const warningEl = document.getElementById("stale-data-warning");
  try {
    const res = await fetch(`${apiBaseUrl}/api/sources`);
    const sources = await res.json();
    const firms = sources.find((s) => s.key === "firms");
    if (!firms || !firms.last_success_at) {
      warningEl.style.display = "block";
      warningEl.textContent = "⚠ No se ha podido confirmar la última actualización de FIRMS.";
      return;
    }
    const ageHours = (Date.now() - new Date(firms.last_success_at).getTime()) / 3600000;
    if (ageHours > STALE_DATA_THRESHOLD_HOURS) {
      warningEl.style.display = "block";
      warningEl.textContent = `⚠ Los datos de detección tienen más de ${Math.round(ageHours)}h de antigüedad - puede haber incendios recientes sin reflejar.`;
    } else {
      warningEl.style.display = "none";
    }
  } catch {
    // Silently skip - this is a secondary heads-up, not core functionality;
    // a failed freshness check shouldn't itself alarm the user.
  }
}

// Renders the recency legend from RECENCY_LEGEND itself (rather than
// hand-duplicating the same 4 colors/labels in index.html) so the on-screen
// legend can never silently drift out of sync with the actual dot colors.
function renderRecencyLegend() {
  const el = document.getElementById("recency-legend");
  if (!el) return;
  const items = [...RECENCY_LEGEND.map((b) => ({ color: b.color, label: b.label })), { color: RECENCY_STALE_COLOR, label: "72 h+" }];
  el.innerHTML = items
    .map((item) => `<span class="recency-swatch"><span class="recency-swatch-dot" style="background:${item.color};"></span>${item.label}</span>`)
    .join("");
}

(async function init() {
  renderRecencyLegend();
  await loadConfig();
  await loadFires();
  checkStaleData();
  setInterval(checkStaleData, 10 * 60 * 1000);
  // loadFires() -> loadIncidents() has already seeded notifiedCriticalIncidentIds
  // with every currently-active critical incident by this point (see
  // notifyNewCriticalIncidents), so re-enabling here on a page reload won't
  // immediately re-notify for fires the user already knows about.
  if (localStorage.getItem(LOCATION_ALERTS_STORAGE_KEY) === "1") {
    document.getElementById("location-alerts-toggle").checked = true;
    enableLocationAlerts();
  }
})();
