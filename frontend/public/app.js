const statusEl = document.getElementById("status");
let apiBaseUrl = "http://localhost:8000";

// Default zoom control (topleft) sits exactly under #incident-sidebar (also
// anchored top:14px/left:14px, full height) - blocked its clicks entirely,
// mouse-wheel zoom was the only way in. #control-rail/its popovers only
// anchor top:14px, not bottom, so bottomright is the one corner neither
// overlay covers.
const map = L.map("map", { zoomControl: false }).setView([40.0, -3.7], 6); // centered on Spain
L.control.zoom({ position: "bottomright" }).addTo(map);

// CARTO Positron - light, neutral basemap (no API key) that matches this
// app's consumer-map design direction (Google/Apple Maps-style light mode)
// far better than a standard OSM street layer, which is busier/more
// saturated than this UI's flat white cards want to sit on top of.
const positronLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  maxZoom: 20,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
});
// Windy-style outdoor/topo basemap - free, no key, standard attribution required.
const topoLayer = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
  maxZoom: 17,
  attribution: '&copy; OpenStreetMap contributors, SRTM | &copy; <a href="https://opentopomap.org">OpenTopoMap</a>',
});
let currentBaseLayer = positronLayer;
currentBaseLayer.addTo(map);

// SIGPAC parcel boundaries (FEGA's official WMS, free/no key, confirmed
// live) - meaningless/visually noisy at anything but a close zoom, so
// minZoom keeps Leaflet from even requesting tiles until it'd actually be
// readable, matching how fuegoscyl.es gates the same layer.
const sigpacLayer = L.tileLayer.wms("https://sigpac-hubcloud.es/wms/ows", {
  layers: "AU.Sigpac:recinto",
  format: "image/png",
  transparent: true,
  version: "1.3.0",
  minZoom: 15,
  attribution: "SIGPAC / FEGA",
});

// Explicit panes so stacking order is guaranteed by z-index, not by DOM
// insertion order. Without this, the hull polygon (SVG) and hotspot dots
// (canvas) both land in Leaflet's shared default overlayPane, and whichever
// renderer's container happens to get created first in the DOM sits on top -
// in practice the hull's semi-transparent gray fill was landing above the
// dots and washing out their real recency colors.
map.createPane("hullPane").style.zIndex = 350; // below overlayPane (400)
map.createPane("hotspotPane").style.zIndex = 450; // above overlayPane, below shadowPane (500)

const markersLayer = L.layerGroup().addTo(map);
const webcamsLayer = L.layerGroup();
const aircraftLayer = L.layerGroup();
const windLayer = L.layerGroup().addTo(map);
// Windy-style arrow field (many points, not just one per active incident -
// see windLayer above) for the current viewport - see reloadWindField.
const windFieldLayer = L.layerGroup().addTo(map);
// Cumulative EFFIS burnt-area extent since Jan 1 of the current year -
// deliberately independent of the date-range selector/timeline scrubber
// (see reloadSeasonBurntArea): this answers "how much has burned this
// campaign", not "what's active right now".
const seasonBurntAreaLayer = L.layerGroup();

// Shared canvas renderer for hotspot dots. Leaflet's default SVG renderer
// creates one DOM node per circleMarker, which starts to choke once you get
// into the thousands of simultaneously-rendered shapes (a full-Spain,
// 30-day view can hold 10k+ raw detections - see the /api/fires point counts
// this was tuned against). Canvas draws every dot into a single <canvas>
// element instead, so plotting every real detection at every zoom level
// (rather than decimating into grid-cell blobs at low zoom, which used to
// hide the fire's spread-direction "comet tail" density Pyrofire shows) stays
// smooth. Created once and reused so every hotspot dot shares one canvas
// instead of Leaflet allocating a new one per marker.
const hotspotRenderer = L.canvas({ padding: 0.5, pane: "hotspotPane" });

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
  const nextLayer = style === "topo" ? topoLayer : positronLayer;
  if (nextLayer === currentBaseLayer) return;
  const satelliteShowing = document.getElementById("satellite-toggle").checked && satelliteLayer;
  if (!satelliteShowing) map.removeLayer(currentBaseLayer);
  currentBaseLayer = nextLayer;
  if (!satelliteShowing) currentBaseLayer.addTo(map);
}

// Fetched once per toggle-on, not re-fetched on pan/zoom/scrubber move -
// burnt-area polygons don't move, and this layer is intentionally scoped to
// "this campaign" rather than whatever window the map's date-range selector
// happens to be on (see seasonBurntAreaLayer above).
async function reloadSeasonBurntArea() {
  seasonBurntAreaLayer.clearLayers();
  const jan1 = new Date(Date.UTC(new Date().getUTCFullYear(), 0, 1));
  const hoursSinceJan1 = Math.ceil((Date.now() - jan1.getTime()) / 3600000);
  const res = await fetch(`${apiBaseUrl}/api/fires?source=EFFIS&hours=${hoursSinceJan1}`);
  if (!res.ok) return;
  const fires = await res.json();
  fires.forEach((fire) => {
    if (!fire.geometry_geojson) return;
    let geometry;
    try {
      geometry = JSON.parse(fire.geometry_geojson);
    } catch {
      return;
    }
    if (geometry.type !== "Polygon" && geometry.type !== "MultiPolygon") return;
    const popupHtml =
      `<div class="card-title">Área quemada (campaña)</div>` +
      `<div class="card-meta">Detectado &nbsp;${fire.acquired_at}<br/>` +
      (fire.area_ha != null ? `Área afectada &nbsp;${fire.area_ha.toLocaleString()} ha` : "") +
      `</div>`;
    // Same flat muted-fill convention as the incident hull polygons
    // (see drawHullShape in renderMap()) - this represents cumulative
    // extent, not per-point recency, so it deliberately doesn't use
    // recencyColor() the way the in-window EFFIS polygons do.
    L.geoJSON(geometry, {
      style: { color: POLYGON_OUTLINE, weight: 2, fillColor: "#8a8577", fillOpacity: 0.14 },
    })
      .bindPopup(popupHtml)
      .addTo(seasonBurntAreaLayer);
  });
}

function toggleSigpacLayer() {
  const toggle = document.getElementById("sigpac-toggle");
  if (toggle.checked) sigpacLayer.addTo(map);
  else map.removeLayer(sigpacLayer);
}

function toggleSeasonBurntAreaLayer() {
  const toggle = document.getElementById("season-burnt-area-toggle");
  if (toggle.checked) {
    seasonBurntAreaLayer.addTo(map);
    reloadSeasonBurntArea();
  } else {
    map.removeLayer(seasonBurntAreaLayer);
  }
}

// Data source (FIRMS/EFFIS/satellite instrument) is intentionally not shown -
// styling only encodes recency (color). Every hotspot dot renders at the same
// fixed radius regardless of how many nearby detections it represents, so the
// ONLY thing that varies across dots is color - which lets a red-to-yellow
// gradient across a cluster read as "this is the direction the fire has been
// spreading" (red = recent edge, yellow = where it started). Encoding a
// second variable (size = detection count) on top of that would compete with
// and muddy the color signal.
// Two DIFFERENT stroke colors, not one shared constant - they have opposite
// requirements. A polygon outline is drawn ONCE per fire event, so a dark
// stroke (contrast against the light CARTO tiles) is exactly right. An
// individual hotspot dot's ring is drawn once PER DETECTION though, and a
// hotspot that keeps re-detecting at nearly the same coordinates for days
// (e.g. a small, long-lived 2ha fire with 900+ detections) stacks hundreds
// of opaque dark 1px rings on the exact same pixels - which accumulates
// into a solid black blob that hides the recency-color fill entirely
// (confirmed live on Niebla/Huelva: 942 detections, 21 days active, once
// the basemap went light enough to make that obvious). A light dot ring
// keeps overlapping dots blending into warm fill colors instead of black.
const POLYGON_OUTLINE = "#333";
const DOT_RING_COLOR = "#fff8f0";
const REPORT_COLOR = "#2dd4bf";
const HOTSPOT_DOT_RADIUS = 4; // fixed for every dot at every zoom - see note above

function setStatus(text) {
  statusEl.textContent = text;
}

// FIXED absolute age buckets (not relative to whatever date-range window
// happens to be selected - an 18h-old detection should look the same whether
// you're browsing "Last 24h" or "Last 7 days"). Tried a finer 7-bucket
// red-to-violet ramp aligned to the 3h FIRMS poll cadence (backend/app/
// config.py's fetch_interval_minutes) - reverted live: FIRMS doesn't
// actually refresh often/evenly enough (see that config comment - combined
// VIIRS/MODIS passes cluster into ~2 windows/day, not continuous coverage)
// for sub-12h buckets to carry real signal, and splitting the "recent"
// range that finely diluted exactly the red/orange contrast that matters
// most here - reading which edge of a fire is its actively advancing front.
// Four visually distinct hues (matching the <12h/<24h/<48h/<72h convention
// other fire-monitoring maps use, e.g. Bseed WATCH/Pyrofire) plus a flat
// gray for anything older keeps that red/orange contrast concentrated where
// it's actually useful - matching RECENCY_LEGEND below.
const RECENCY_LEGEND = [
  // The freshest bucket gets its own extra-saturated red AND a bigger dot
  // (see FRESH_RECENCY_MAX_HOURS/hotspotMarker below) - these are the
  // detections that matter most for "where is the active front right now",
  // so they need to visually punch through the rest of the pack, not just
  // sit as another shade in the red family.
  { maxHours: 6, color: "#99000D", label: "< 6 h" },
  { maxHours: 12, color: "#ef4444", label: "6-12 h" },
  { maxHours: 24, color: "#f97316", label: "12-24 h" },
  { maxHours: 48, color: "#eab308", label: "24-48 h" },
  { maxHours: 72, color: "#3b82f6", label: "48-72 h" },
];
const FRESH_RECENCY_MAX_HOURS = RECENCY_LEGEND[0].maxHours;
const RECENCY_STALE_COLOR = "#6b7280"; // older than the oldest bucket (72h+) - clearly "cold", not another shade of the active-fire palette

function recencyColor(acquiredAtIso) {
  const ageHours = (Date.now() - new Date(acquiredAtIso).getTime()) / 3600000;
  const bucket = RECENCY_LEGEND.find((b) => ageHours <= b.maxHours);
  return bucket ? bucket.color : RECENCY_STALE_COLOR;
}

// Distinct marker SHAPE per satellite source, layered on top of the existing
// recency COLOR - otherwise a EUMETSAT (geostationary, ~2km native pixel) or
// Sentinel-3 (polar-orbiting, its own separate overpass schedule from FIRMS'
// VIIRS/MODIS) detection is visually indistinguishable from a FIRMS one,
// even though they're different instruments with different confidence
// characteristics. FIRMS is by far the highest-volume source (thousands of
// points - see hotspotRenderer above) and stays on the canvas circleMarker
// path; EUMETSAT/Sentinel-3 have far fewer detections at any given time, so
// a plain DOM divIcon (fixed CSS pixel shape, no canvas path needed) is
// simple and cheap at their scale. Returns null for FIRMS/anything else -
// callers fall back to the existing circleMarker in that case.
const SOURCE_SHAPE_SIZE = HOTSPOT_DOT_RADIUS * 2 + 2;

function sourceShapeIcon(source, fillColor, ringColor, ringWeight) {
  const s = SOURCE_SHAPE_SIZE;
  const border = `${ringWeight}px solid ${ringColor}`;
  let html;
  if (source === "EUMETSAT") {
    // Triangle via a CSS border trick - no SVG/canvas path needed for a fixed-size DOM shape.
    html =
      `<div style="width:0;height:0;` +
      `border-left:${s / 2}px solid transparent;border-right:${s / 2}px solid transparent;` +
      `border-bottom:${s}px solid ${fillColor};filter:drop-shadow(0 0 0.5px ${ringColor});"></div>`;
  } else if (source === "SENTINEL3") {
    html = `<div style="width:${s}px;height:${s}px;background:${fillColor};border:${border};transform:rotate(45deg);box-sizing:border-box;"></div>`;
  } else {
    return null;
  }
  return L.divIcon({ className: "", html, iconSize: [s, s], iconAnchor: [s / 2, s / 2] });
}

// Builds either a shaped divIcon marker (EUMETSAT/Sentinel-3) or the default
// canvas circleMarker (FIRMS/everything else) for one raw fire detection -
// used at every place a raw hotspot dot gets drawn, so source shape stays
// consistent whether it's part of a loose fragment or the main dot layer.
function hotspotMarker(fire, { radius, ringColor, ringWeight, renderer }) {
  const fillColor = recencyColor(fire.acquired_at);
  const ageHours = (Date.now() - new Date(fire.acquired_at).getTime()) / 3600000;
  // The <6h bucket renders larger, not just redder - it's the leading edge
  // of the fire right now, and at a shared fixed radius it read as just
  // another dot in the red cluster instead of the thing to look at first.
  const isFresh = ageHours <= FRESH_RECENCY_MAX_HOURS;
  const effectiveRadius = isFresh ? radius * 1.6 : radius;
  const icon = sourceShapeIcon(fire.source, fillColor, ringColor, ringWeight);
  if (icon) {
    return L.marker([fire.latitude, fire.longitude], { icon });
  }
  return L.circleMarker([fire.latitude, fire.longitude], {
    renderer,
    radius: effectiveRadius,
    color: ringColor,
    weight: ringWeight,
    fillColor,
    // Not fully opaque - a long-running fire with thousands of detections
    // packed into a small area (e.g. an incident active for weeks) paints
    // hundreds of overlapping same-radius dots on the same few screen
    // pixels; at full opacity the canvas painter's algorithm just shows
    // whichever dot happened to be drawn last, so a tiny zoom change (which
    // shifts pixel rounding) can make a completely different dot "win" and
    // the rest look like they vanished. Partial opacity lets overlapping
    // dots alpha-blend into a visibly denser/darker patch instead - an
    // honest "lots of activity here" signal that isn't order-dependent,
    // rather than occluding down to a single arbitrary dot.
    fillOpacity: 0.6,
  });
}

// NOTE: this used to grid-bucket nearby point detections into a single dot
// per cell purely to cap how many SVG shapes got drawn at country/region-wide
// zoom levels (lastFires isn't viewport-filtered, so a wide view can hold
// several thousand detections). That performance decimation was the reason
// zoomed-out views looked sparse - one blob per grid cell instead of the
// dense, directional "comet tail" of individual colored dots other
// fire-monitoring tools (e.g. Pyrofire) show at every zoom. Switching hotspot
// dots to Leaflet's canvas renderer (see hotspotRenderer above) removes the
// need for it entirely: a single canvas element handles the full real point
// count (measured up to ~11.5k for a 30-day full-Spain view) smoothly, so
// every raw detection now renders as its own small dot at every zoom level -
// see the single rendering pass in renderMap() below.

// Groups fires by real-world proximity (chain-linkage union-find), not by a
// fixed grid cell - so hotspots that belong to the same spreading fire merge
// into ONE region regardless of how the display grid happens to slice them.
// This is what backs the affected-area polygon and its single geocode button -
// unrelated to how individual hotspot dots are plotted (every raw detection
// gets its own dot now; see hotspotRenderer above).
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

// A handful of points strung out along a near-straight line still forms a
// valid hull - just a degenerate needle-thin one (near-zero area for its
// perimeter). Polsby-Popper compactness (4*pi*area / perimeter^2) is 1.0 for
// a circle and drops toward 0 for a sliver; a real fire-shaped blob
// comfortably clears this even when it's fairly elongated (e.g. a
// valley-following fire or a long connecting corridor between two denser
// patches).
//
// Originally 0.06, tuned back when a hull was built per dense sub-cluster
// (see the "ONE hull over the WHOLE chain-linked group" comment above the
// renderMap() hull call). Now that a single hull spans an incident's entire
// chain-linked shape - dense core plus long, thin connecting corridors to
// outlying detections, by design - the whole-group shape is naturally less
// circular than a lone dense blob, even for a real, large, well-established
// fire. Confirmed live (2026-07-20): La Mierla (Guadalajara, 2994
// detections, clearly a real multi-week fire with its own dense core plus a
// Villares-de-Jadraque connecting arm) computed a whole-group compactness of
// ~0.057 - just under the old 0.06 - which silently dropped its polygon
// entirely (raw dots still rendered, no shape, no click-through to the
// incident). A genuinely degenerate case checked the same way - Sahagún
// (incident 237), just 3 points where two sit ~300m apart and the third
// ~3km off, forming a real needle-thin sliver triangle - computed ~0.049.
// 0.05 sits between the two: still rejects Sahagún's sliver, no longer
// rejects La Mierla's real elongated/branching extent (and Luesia, another
// real multi-week fire with a similar branching shape, already cleared both
// values comfortably at ~0.072).
const MIN_HULL_COMPACTNESS = 0.05;

// A compact-enough triangle from just 3-4 points a couple hundred meters
// apart still passes MIN_HULL_COMPACTNESS but reads as a stray, meaningless
// sliver of "shape" rather than a real fire extent (confirmed live
// 2026-07-20: several such tiny triangles/wedges scattered around a real
// incident, each from a handful of nearby points, that added visual noise
// without conveying anything a plain dot cluster wouldn't already show).
// Below this real-world area, skip the polygon entirely - the underlying
// points still render as individual dots regardless (see visiblePointFires
// below), so nothing about the data disappears, just the misleading "shape"
// claim over too few points to support one.
const MIN_HULL_AREA_HA = 3;

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

// GeoJSON coordinates are nested arrays of [lon, lat] pairs - a Polygon's
// coordinates are "array of rings" (exterior + optional holes), a
// MultiPolygon's are "array of polygons, each an array of rings". Both
// nestings already match what L.polygon() itself accepts (it auto-detects
// simple ring vs ring-with-holes vs multipolygon by nesting depth), so the
// only conversion needed is swapping each [lon, lat] leaf pair to Leaflet's
// [lat, lon] order - recursing until a leaf (an array of two numbers) is hit.
function geojsonCoordsToLatLngs(coords) {
  if (Array.isArray(coords[0]) && typeof coords[0][0] === "number") {
    return coords.map(([lon, lat]) => [lat, lon]);
  }
  return coords.map(geojsonCoordsToLatLngs);
}

// Calls the backend's real-water-body subtraction (see routers/geo.py's
// POST /api/geo/subtract-water, services/geo_filter.py's
// water_geometry_near) to cut any real lake/reservoir the hull's shortest
// path crosses out as a hole or notch - the KNOWN CAVEAT documented above.
// Returns null ("nothing to change, keep rendering the plain hull") on ANY
// failure - network error, non-OK response, or the backend simply finding
// no water nearby - so a flaky call or a slow first-time Corine tile fetch
// can only skip this refinement, never break the hull rendering itself.
async function waterSubtractedHullLatLngs(hullLatLon) {
  try {
    const res = await fetch(`${apiBaseUrl}/api/geo/subtract-water`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points: hullLatLon }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    if (!data.subtracted || !data.geometry) return null;
    return geojsonCoordsToLatLngs(data.geometry.coordinates);
  } catch {
    return null;
  }
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
// A run of at least this many consecutive zero-detection days is treated as
// a genuine dead stretch (as opposed to normal day-to-day noise) when
// looking for the fire's real onset - see the onset-detection comment in
// dailyDetectionCounts below.
const DAILY_CHART_GAP_DAYS = 5;

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

  // Detect the REAL onset of sustained fire activity instead of always
  // windowing from the incident's absolute first day. merge_reassociable_incidents
  // (backend/app/services/incidents.py) can fold an old, small, unrelated
  // incident into a real fire's record purely because their centroids end
  // up within INCIDENT_REASSOCIATION_DEG of each other (up to
  // ARCHIVED_REASSOCIATION_MAX_AGE_DAYS = 45 days apart) - since
  // first_detected_at is then the min() across the merge, that stray
  // handful of weeks-old detections can pull this chart's start far ahead
  // of when the fire itself actually ignited, leaving 2-3 weeks of
  // near-zero noise before the real ramp-up (confirmed live: "Villarino de
  // los Aires" showed ~15-20 flat days before its real, 374-detection
  // spike). A run of DAILY_CHART_GAP_DAYS+ consecutive zero-count days
  // followed by renewed activity marks such a dead stretch; keep only the
  // LAST one found (there could be more than one) so the window starts
  // right before the real ramp, not at the literal earliest detection ever
  // merged into this incident.
  let onsetIndex = 0;
  let zeroRun = 0;
  for (let i = 0; i < series.length; i++) {
    if (series[i].count === 0) {
      zeroRun++;
    } else {
      if (zeroRun >= DAILY_CHART_GAP_DAYS) onsetIndex = i;
      zeroRun = 0;
    }
  }
  // onsetIndex - 1 is already a real zero-count day whenever a gap was
  // found (it's part of the run that triggered it), so it doubles as the
  // "day before" padding below - no synthetic day needed in that case.
  const windowed = onsetIndex > 0 ? series.slice(onsetIndex - 1) : series;

  // Pad with a real zero-detection day immediately before the first real one
  // (always true - there were 0 detections for this incident the day before
  // its very first one) so the line/area visibly rises FROM zero instead of
  // starting mid-height at the chart's left edge. Only needed as a synthetic
  // day when the onset detection above didn't already supply one.
  if (onsetIndex === 0) {
    const dayBefore = new Date(first);
    dayBefore.setUTCDate(dayBefore.getUTCDate() - 1);
    windowed.unshift({ day: dayBefore.toISOString().slice(0, 10), count: 0 });
  }

  // Same on the trailing end, but ONLY if that day has actually elapsed with
  // zero detections - the day after "today" hasn't happened yet, so padding
  // a still-active, still-growing fire with a fake "tomorrow = 0" would
  // falsely imply it had already stopped.
  const todayKey = new Date().toISOString().slice(0, 10);
  const dayAfter = new Date(last);
  dayAfter.setUTCDate(dayAfter.getUTCDate() + 1);
  const dayAfterKey = dayAfter.toISOString().slice(0, 10);
  if (dayAfterKey < todayKey) windowed.push({ day: dayAfterKey, count: 0 });

  // Long-running incidents (up to INCIDENTS_WINDOW_HOURS = 30 days), or ones
  // whose real active stretch itself runs long, would otherwise render
  // unreadably thin bars - keep only the most recent DAILY_CHART_MAX_BARS
  // days of the (already onset-trimmed) window rather than silently
  // mis-scaling every bar.
  return windowed.slice(-DAILY_CHART_MAX_BARS);
}

// Catmull-Rom -> cubic Bezier smoothing for an OPEN line (unlike the map's
// own smoothRing/catmull helpers, which smooth a closed polygon loop) - each
// segment only looks at its own two immediate neighbors, so it can't
// overshoot past a local min/max the way a naive global spline could.
function smoothLinePath(points) {
  if (points.length < 3) return `M ${points.map((p) => p.join(",")).join(" L ")}`;
  let d = `M ${points[0][0]},${points[0][1]} `;
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[Math.max(0, i - 1)];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[Math.min(points.length - 1, i + 2)];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += `C ${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)} `;
  }
  return d;
}

function dayLabel(dayKey) {
  return new Date(dayKey + "T00:00:00Z").toLocaleDateString("es-ES", { day: "numeric", month: "short" });
}

// Reserved headroom above the line for the peak day's direct value label -
// selective (only the peak, not every point) per the app's dataviz conventions.
const DAILY_CHART_LABEL_HEADROOM = 16;
const DAILY_CHART_PLOT_AREA = DAILY_CHART_HEIGHT - DAILY_CHART_LABEL_HEADROOM;
// How many date ticks to show under the axis regardless of how many days the
// series spans - a 21-day incident showing only its first/last day made every
// day in between impossible to identify without hovering each point. Evenly
// spaced (not one per day) so labels never overlap in the 280px-wide sidebar.
const DAILY_CHART_MAX_TICKS = 5;

function dailyActivityChartHtml(events) {
  const series = dailyDetectionCounts(events);
  if (series.length < 2) return "";

  const maxCount = Math.max(...series.map((d) => d.count), 1);
  const peakIndex = series.reduce((best, d, i) => (d.count > series[best].count ? i : best), 0);
  const stepX = DAILY_CHART_WIDTH / (series.length - 1);
  const xAt = (i) => i * stepX;
  const yAt = (count) => DAILY_CHART_HEIGHT - (count / maxCount) * DAILY_CHART_PLOT_AREA;

  const points = series.map((d, i) => [xAt(i), yAt(d.count)]);
  const linePath = smoothLinePath(points);
  const areaPath = `${linePath} L ${xAt(series.length - 1).toFixed(1)},${DAILY_CHART_HEIGHT} L 0,${DAILY_CHART_HEIGHT} Z`;

  const dots = series
    .map((d, i) => {
      const x = xAt(i);
      const y = yAt(d.count);
      const peakLabel =
        i === peakIndex && d.count > 0
          ? `<text x="${x.toFixed(1)}" y="${Math.max(10, y - 7).toFixed(1)}" text-anchor="middle" class="daily-chart-peak-label">${d.count}</text>`
          : "";
      // Same class/data-* attributes the existing shared tooltip (see
      // showDailyChartTooltip) already reads - point markers just replace
      // bars as the hoverable element, no tooltip logic changes needed.
      return (
        `<circle class="daily-chart-bar" data-label="${dayLabel(d.day)}" data-count="${d.count}" ` +
        `cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3"/>` +
        peakLabel
      );
    })
    .join("");

  // Evenly spaced tick indices, always including the first and last day -
  // Set dedupes in case DAILY_CHART_MAX_TICKS >= series.length (short spans
  // just get one tick per day).
  const tickCount = Math.min(DAILY_CHART_MAX_TICKS, series.length);
  const tickIndices = Array.from(
    new Set(Array.from({ length: tickCount }, (_, i) => Math.round((i * (series.length - 1)) / (tickCount - 1))))
  );
  const ticks = tickIndices
    .map((i) => {
      const x = xAt(i);
      const anchor = i === 0 ? "start" : i === series.length - 1 ? "end" : "middle";
      return `<text x="${x.toFixed(1)}" y="10" text-anchor="${anchor}" class="daily-chart-tick-label">${dayLabel(series[i].day)}</text>`;
    })
    .join("");

  return (
    `<div class="sparkline-wrap">` +
    `<div class="sparkline-label">Detecciones por día</div>` +
    `<svg viewBox="0 0 ${DAILY_CHART_WIDTH} ${DAILY_CHART_HEIGHT}" class="sparkline-svg daily-chart-svg" preserveAspectRatio="none">` +
    `<line x1="0" y1="${DAILY_CHART_HEIGHT - 0.5}" x2="${DAILY_CHART_WIDTH}" y2="${DAILY_CHART_HEIGHT - 0.5}" stroke="var(--border-soft)" stroke-width="1"/>` +
    `<path d="${areaPath}" fill="var(--accent)" opacity="0.16" stroke="none"/>` +
    `<path d="${linePath}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` +
    dots +
    `</svg>` +
    `<div class="daily-chart-ticks"><svg viewBox="0 0 ${DAILY_CHART_WIDTH} 16" class="daily-chart-ticks-svg" preserveAspectRatio="none">${ticks}</svg></div>` +
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

// In-flight request de-duplication: an incident with thousands of raw
// detections (e.g. La Mierla, ~2994 points) now attaches this SAME
// per-incident data (geocode/telegram/regional/satellite) to every one of
// its dots, not just its polygon - see incidentInfoByFire in renderMap().
// Without this, every dot mounted in the same synchronous pass would race to
// fetch before the first request resolves and populates the cache, firing
// one duplicate network call per dot instead of one per incident. Callers
// that arrive after the first one just await the SAME pending promise.
const pendingGeocode = new Map();

async function getGeocode(lat, lon) {
  const key = geocodeCacheKey(lat, lon);
  if (geocodeCache.has(key)) return geocodeCache.get(key);
  if (pendingGeocode.has(key)) return pendingGeocode.get(key);
  const promise = (async () => {
    const res = await fetch(`${apiBaseUrl}/api/geocode?lat=${lat}&lon=${lon}`);
    return res.json();
  })().then((data) => {
    geocodeCache.set(key, data);
    pendingGeocode.delete(key);
    return data;
  });
  pendingGeocode.set(key, promise);
  return promise;
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
  badge:
    `<svg ${ICON_SVG_ATTRS} width="13" height="13" class="icon"><circle cx="12" cy="9" r="6"/><path d="M9 14l-2 7 5-3 5 3-2-7"/></svg>`,
};

// Cache of Telegram messages matched to an incident (parallel to
// geocodeCache below) so re-opening the same polygon's popup doesn't re-fetch.
const telegramCache = new Map();

// See pendingGeocode above for why this de-duplication exists.
const pendingTelegram = new Map();

async function getTelegramMentions(incidentId) {
  if (telegramCache.has(incidentId)) return telegramCache.get(incidentId);
  if (pendingTelegram.has(incidentId)) return pendingTelegram.get(incidentId);
  const promise = (async () => {
    const res = await fetch(`${apiBaseUrl}/api/telegram/messages?incident_id=${incidentId}`);
    return res.json();
  })().then((data) => {
    telegramCache.set(incidentId, data);
    pendingTelegram.delete(incidentId);
    return data;
  });
  pendingTelegram.set(incidentId, promise);
  return promise;
}

// Cache of official regional-government status records matched to an
// incident (parallel to telegramCache) so re-opening the same polygon's
// popup doesn't re-fetch.
const regionalCache = new Map();

// See pendingGeocode above for why this de-duplication exists.
const pendingRegional = new Map();

async function getRegionalStatus(incidentId) {
  if (regionalCache.has(incidentId)) return regionalCache.get(incidentId);
  if (pendingRegional.has(incidentId)) return pendingRegional.get(incidentId);
  const promise = (async () => {
    const res = await fetch(`${apiBaseUrl}/api/regional-incidents?incident_id=${incidentId}`);
    return res.json();
  })().then((data) => {
    regionalCache.set(incidentId, data);
    pendingRegional.delete(incidentId);
    return data;
  });
  pendingRegional.set(incidentId, promise);
  return promise;
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

// See pendingGeocode above for why this de-duplication exists.
const pendingSatellite = new Map();

async function getSatelliteScenes(incidentId) {
  if (satelliteCache.has(incidentId)) return satelliteCache.get(incidentId);
  if (pendingSatellite.has(incidentId)) return pendingSatellite.get(incidentId);
  const promise = (async () => {
    const res = await fetch(`${apiBaseUrl}/api/copernicus/scenes?incident_id=${incidentId}`);
    return res.json();
  })().then((data) => {
    satelliteCache.set(incidentId, data);
    pendingSatellite.delete(incidentId);
    return data;
  });
  pendingSatellite.set(incidentId, promise);
  return promise;
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

// Cache of vegetation/burnt-area stats matched to an incident (parallel to
// telegramCache/regionalCache/satelliteCache) - only ever non-null for
// incidents with a Copernicus EMS activation (see
// get_incident_vegetation_stats in the backend), a small minority, so most
// incidents' entry here is a cached `null` rather than never being fetched
// twice for nothing.
const vegetationCache = new Map();
const pendingVegetation = new Map();

async function getVegetationStats(incidentId) {
  if (vegetationCache.has(incidentId)) return vegetationCache.get(incidentId);
  if (pendingVegetation.has(incidentId)) return pendingVegetation.get(incidentId);
  const promise = (async () => {
    const res = await fetch(`${apiBaseUrl}/api/incidents/${incidentId}/vegetation`);
    return res.json();
  })().then((data) => {
    vegetationCache.set(incidentId, data);
    pendingVegetation.delete(incidentId);
    return data;
  });
  pendingVegetation.set(incidentId, promise);
  return promise;
}

// Same 3-step palette cycled by rank, not a per-category color (the actual
// CORINE-style labels - "matorral", "bosque", "pastos"...- vary too much to
// hand-map every one, and there are never more than 3 shown, see
// top_land_use's [:3] cap in the backend).
const VEGETATION_BAR_COLORS = ["var(--accent)", "var(--degraded)", "var(--wind)"];

function vegetationSectionHtml(stats) {
  if (!stats || !stats.top_land_use || stats.top_land_use.length === 0) return "";
  const maxHa = Math.max(...stats.top_land_use.map((row) => row.hectares));
  const rows = stats.top_land_use
    .map(
      (row, i) =>
        `<div class="veg-row">` +
        `<span class="veg-swatch" style="background:${VEGETATION_BAR_COLORS[i]};"></span>` +
        `<span class="veg-label">${row.label}</span>` +
        `<span class="veg-bar-wrap"><span class="veg-bar" style="width:${((row.hectares / maxHa) * 100).toFixed(0)}%; background:${VEGETATION_BAR_COLORS[i]};"></span></span>` +
        `<span class="veg-ha">${Math.round(row.hectares).toLocaleString("es-ES")} ha</span>` +
        `</div>`
    )
    .join("");
  return (
    `<div class="vegetation-card">` +
    `<div class="vegetation-card-title">${ICONS.flame} Vegetación quemada` +
    (stats.burnt_area_ha != null ? ` <span class="veg-total">${Math.round(stats.burnt_area_ha).toLocaleString("es-ES")} ha totales</span>` : "") +
    `</div>` +
    rows +
    `<div class="legend-hint">Fuente: activación Copernicus EMS (cartografía oficial de emergencia).</div>` +
    `</div>`
  );
}

// Current-hour reading from the same Open-Meteo series enableIncidentPrediction
// already fetches for the fire-spread wind scrubber (index 0 = current hour) -
// temperature/humidity ride along on that same request/response
// (fetch_wind_series, backend/app/services/fire_spread.py), not a second
// weather source, so this never needs its own network call.
function weatherSectionHtml(hour, airQuality) {
  if (!hour) return "";
  const windArrow = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="transform:rotate(${hour.wind_direction_from_deg}deg);"><path d="M12 2v18M12 2l-5 5M12 2l5 5"/></svg>`;
  // 120m/CAPE/VPD/etc. are additive extras from fetch_wind_series - guarded
  // individually since older cached prediction responses (or a future
  // upstream Open-Meteo hiccup on just one field) may not carry all of them.
  const extraItems = [
    hour.wind_120m_speed_kmh != null
      ? `<span class="weather-item">🌬️ ${Math.round(hour.wind_120m_speed_kmh)} km/h a 120m</span>`
      : "",
    hour.cloudcover_pct != null ? `<span class="weather-item">☁️ ${Math.round(hour.cloudcover_pct)}% nubes</span>` : "",
    hour.precipitation_mm != null ? `<span class="weather-item">🌧️ ${hour.precipitation_mm.toFixed(1)} mm</span>` : "",
    hour.solar_radiation_wm2 != null
      ? `<span class="weather-item">☀️ ${Math.round(hour.solar_radiation_wm2)} W/m²</span>`
      : "",
    hour.vapor_pressure_deficit_kpa != null
      ? `<span class="weather-item">🍃 VPD ${hour.vapor_pressure_deficit_kpa.toFixed(1)} kPa</span>`
      : "",
    hour.cape_jkg != null ? `<span class="weather-item">⚡ CAPE ${Math.round(hour.cape_jkg)} J/kg</span>` : "",
    hour.wet_bulb_c != null ? `<span class="weather-item">🌡️💧 ${hour.wet_bulb_c}°C bulbo húmedo</span>` : "",
    airQuality?.european_aqi != null ? `<span class="weather-item">🫁 AQI ${Math.round(airQuality.european_aqi)}</span>` : "",
  ]
    .filter(Boolean)
    .join("");
  return (
    `<div class="weather-card">` +
    `<div class="weather-card-title">Previsión ahora</div>` +
    `<div class="weather-row">` +
    `<span class="weather-item" style="color:var(--wind);">${windArrow} ${Math.round(hour.wind_speed_kmh)} km/h</span>` +
    `<span class="weather-item">🌡️ ${Math.round(hour.temperature_c)}°C</span>` +
    `<span class="weather-item">💧 ${Math.round(hour.humidity_pct)}% hum.</span>` +
    `</div>` +
    (extraItems ? `<div class="weather-row">${extraItems}</div>` : "") +
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
  const title = matchedIncident ? displayName(matchedIncident) : geo ? geo.locality : "Área de incendio estimada";
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
  // A matched incident already has its own, richer view in the sidebar -
  // opening a second, differently-formatted popup on top of it duplicated
  // the same information in two places at once. Clicking the map now jumps
  // straight to that same panel (exactly like clicking it in the sidebar
  // list), so there's only ever one place an incident's detail lives. Only
  // a truly unmatched fire cluster (no backend incident yet - just raw
  // detections) still gets its own lightweight geocode popup below, since
  // there's no sidebar destination to send it to.
  if (matchedIncident) {
    polygon.bindTooltip(displayName(matchedIncident), { sticky: true });
    polygon.on("click", () => showIncidentDetail(matchedIncident));
    return;
  }

  let geo = geocodeCache.get(geocodeCacheKey(earliest.latitude, earliest.longitude)) || null;

  const render = () => regionPopupHtml(group, earliest, mostRecent, geo, null, null, null, null, growth);
  const displayLocality = () => geo && geo.locality;

  polygon.bindPopup(render());
  if (displayLocality()) polygon.bindTooltip(displayLocality(), { sticky: true });

  polygon.on("popupopen", (e) => {
    const container = e.popup.getElement();
    const copyBtn = container.querySelector(".copy-btn");
    if (copyBtn) {
      copyBtn.onclick = () => navigator.clipboard.writeText(copyBtn.dataset.hashtag);
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
}

// ---------- National cluster overview (low/mid zoom) ----------
// The very first thing a user sees - default zoom is 6, a whole-Spain view
// (see the map's own initial setView call) - used to be the same detailed
// per-incident rendering just zoomed out, which read as a scatter of tiny,
// meaningless dots. This replaces that with colored, sized count bubbles
// instead - an immediate "where is it bad right now" read (bigger + redder
// = more detections), same visual language as the classic Leaflet
// marker-cluster convention - across a wide enough zoom range that it stays
// useful while zooming in, not just at the very first paint. Past this
// zoom, the detailed per-incident rendering (polygons, wind arrows,
// individual dots) everything below already handles takes over.
const CLUSTER_OVERVIEW_MAX_ZOOM = 8;
const clusterOverviewLayer = L.layerGroup().addTo(map);

// How far apart (degrees) two incidents can be and still merge into one
// bubble, at zoom 4 (roughly the whole-Spain view) - shrinks as you zoom in
// (see clusterIncidentsForOverview) so bubbles stay meaningfully sized
// instead of a handful of giant blobs covering half the country at the most
// zoomed-out level. Deliberately generous rather than a small fixed grid
// cell: a single large, irregularly-shaped fire's own detections can easily
// span more raw distance than a small cell would cover, which used to
// fragment ONE incident across several adjacent bubbles (confirmed live:
// Niebla's ~2000 detections split into separate green/yellow/orange bubbles
// that were all the same fire). Clustering INCIDENTS (each already a
// correctly-shaped, single real fire - see lastIncidents) rather than raw
// per-detection points is what actually fixes that; the generous merge
// radius on top additionally folds separate-but-nearby incidents together
// for a cleaner national read.
const CLUSTER_GROUP_BASE_DEG = 1.1;

// Thresholds are raw detection counts within one bubble, not incident
// severity - a single very active fire can rack up hundreds/thousands of
// detections on its own, which is exactly the "needs attention" signal this
// overview exists to surface at a glance. No upper bound: a big enough fire
// (or cluster of them) should show its real count, however large, not be
// capped into looking the same as a much smaller one.
function clusterBubbleStyle(count) {
  if (count >= 150) return { color: "#c92a2a", radius: 24 };
  if (count >= 50) return { color: "#e8590c", radius: 20 };
  if (count >= 10) return { color: "#f0b429", radius: 17 };
  return { color: "#40a02b", radius: 14 };
}

// Greedy proximity clustering (same shape as the backend's
// _cluster_recent_points in fire_spread.py) over incident centroids,
// weighted by each incident's own detection_count - both for the merged
// bubble's total count AND for where it's centered (a 2000-detection fire
// pulls the bubble toward itself far more than a 2-detection one 30km away).
function clusterIncidentsForOverview(incidents) {
  const zoom = map.getZoom();
  const clusterDeg = CLUSTER_GROUP_BASE_DEG / Math.pow(1.6, Math.max(0, zoom - 4));
  const remaining = incidents.map((incident) => ({
    lat: incident.centroid_lat,
    lon: incident.centroid_lon,
    count: incident.detection_count,
  }));
  const groups = [];
  while (remaining.length) {
    const seed = remaining.pop();
    const group = [seed];
    for (let i = remaining.length - 1; i >= 0; i--) {
      if (Math.hypot(remaining[i].lat - seed.lat, remaining[i].lon - seed.lon) <= clusterDeg) {
        group.push(remaining[i]);
        remaining.splice(i, 1);
      }
    }
    groups.push(group);
  }
  return groups.map((group) => {
    const totalCount = group.reduce((sum, item) => sum + item.count, 0);
    const lat = group.reduce((sum, item) => sum + item.lat * item.count, 0) / totalCount;
    const lon = group.reduce((sum, item) => sum + item.lon * item.count, 0) / totalCount;
    return { lat, lon, count: totalCount };
  });
}

function renderClusterOverview(incidents) {
  clusterOverviewLayer.clearLayers();
  if (!incidents.length) return;

  clusterIncidentsForOverview(incidents).forEach((cluster) => {
    const style = clusterBubbleStyle(cluster.count);
    const size = style.radius * 2;
    L.marker([cluster.lat, cluster.lon], {
      icon: L.divIcon({
        className: "cluster-bubble-icon",
        html: `<div class="cluster-bubble" style="width:${size}px; height:${size}px; background:${style.color};">${cluster.count.toLocaleString("es-ES")}</div>`,
        iconSize: [size, size],
        iconAnchor: [style.radius, style.radius],
      }),
    })
      .on("click", () => map.setView([cluster.lat, cluster.lon], CLUSTER_OVERVIEW_MAX_ZOOM + 2))
      .addTo(clusterOverviewLayer);
  });
}

// Pure rendering pass over already-fetched data - re-run on zoom changes
// without re-hitting the API (only the cluster-overview/detailed-render
// split above actually depends on zoom now that hotspot dots themselves
// render at a fixed radius everywhere).
function renderMap() {
  const zoom = map.getZoom();

  if (zoom <= CLUSTER_OVERVIEW_MAX_ZOOM) {
    markersLayer.clearLayers();
    incidentEstimatesById.clear();
    // Same risk/status/source filters the sidebar list applies - the
    // overview matches what's actually listed, and picks up this session's
    // detection-count sort for free (not that order matters for bubbles).
    renderClusterOverview(applyFilters(lastIncidents));
    return;
  }
  clusterOverviewLayer.clearLayers();

  markersLayer.clearLayers();
  incidentEstimatesById.clear();
  const hours = getSelectedDays() * 24;

  // While an incident's detail view is open, its own detections always show
  // in full (see selectedIncidentDetections) - only OTHER fires still obey
  // the scrubber/date-range filter. Deduped by id since lastFires can
  // already contain some of the same rows when they fall inside the
  // current filter window.
  const filteredFires = scrubberFilteredFires(lastFires);
  const visibleFires = selectedIncidentDetections.length
    ? [...filteredFires, ...selectedIncidentDetections.filter((d) => !filteredFires.some((f) => f.id === d.id))]
    : filteredFires;

  const pointFires = [];
  visibleFires.forEach((fire) => {
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
        style: { color: POLYGON_OUTLINE, weight: 2, fillColor, fillOpacity: 0.5 },
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
  const filteredOutPoints = new Set();

  // Every raw detection dot's link back to its incident, populated below for
  // ALL proximity groups regardless of whether that group ends up drawing a
  // polygon - a hull can be skipped for plenty of legitimate reasons (fewer
  // than 3 points, failing the compactness/area gates just below), but the
  // incident match itself doesn't depend on any of that. Without this, a dot
  // belonging to a matched incident whose polygon didn't render lost ANY
  // link back to its sidebar/timeline entry - see the bare "Detectado" popup
  // this used to fall back to in the visiblePointFires loop below.
  const incidentInfoByFire = new Map();

  proximityGroups.forEach((group) => {
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
    // (and, below, its underlying raw-detection dots). Groups with no
    // matched incident (no backend FireIncident to filter on) always render,
    // same as before this feature existed.
    if (matchedIncident && !incidentPassesFilters(matchedIncident, getActiveFilters())) {
      group.forEach((f) => filteredOutPoints.add(f));
      return;
    }

    // Identity (name, timeline, matched incident, filters) is decided once
    // for the WHOLE chain-linked group - a fire that jumped a real gap is
    // still one incident. Only the polygon SHAPE is re-clustered below. Computed
    // BEFORE the <3-points hull bail-out (unlike before) so a dot belonging to
    // a tiny or otherwise hull-less group still carries its incident identity -
    // see incidentInfoByFire below.
    const earliest = group.reduce((oldest, f) =>
      new Date(f.acquired_at) < new Date(oldest.acquired_at) ? f : oldest
    );
    const mostRecent = group.reduce(
      (latest, f) => (new Date(f.acquired_at) > new Date(latest) ? f.acquired_at : latest),
      group[0].acquired_at
    );

    // Burnt-area estimate + growth trend computed once for the WHOLE incident
    // (not per visual sub-shape) - see estimateIncidentGrowth. Cached by
    // incident id so the sidebar detail view (which doesn't have the raw
    // point group) can look it up too.
    const growth = matchedIncident ? estimateIncidentGrowth(group) : null;
    if (matchedIncident && growth) incidentEstimatesById.set(matchedIncident.id, growth);

    // Every point in this group can now be given the SAME rich, incident-linked
    // popup a polygon would get (see visiblePointFires below) regardless of
    // whether a polygon actually renders for it - a matched incident should
    // never lose its click-through to the sidebar/timeline just because its
    // hull failed the compactness/area gates below or it's too small (<3
    // points) to hull at all.
    if (matchedIncident) {
      group.forEach((f) => incidentInfoByFire.set(f, { group, earliest, mostRecent, matchedIncident, growth }));
    }

    if (group.length < 3) return; // need at least a triangle for a meaningful hull

    // ONE hull over the WHOLE chain-linked group, not a re-clustered hull per
    // dense sub-pocket stitched together with connector lines - confirmed
    // live (2026-07-20) against real data that concaveHull() already traces
    // a single continuous shape along a sparse connecting corridor (e.g.
    // Luesia's Asín/Orés chain, La Mierla's Villares de Jadraque arm) at the
    // SAME concavity already tuned for the dense core - the previous
    // sub-clustering step was what split those into separate blobs joined by
    // a dashed line, not a limitation of the hull algorithm itself. This is a
    // deliberate reversal of this file's older "never bridge a real gap"
    // stance - the user looked at real incidents, drew the single continuous
    // shape they expected, and confirmed that's the desired behavior even
    // though it does mean the fill can span ground no single detection
    // actually confirmed (same honesty trade-off EFFIS's own rapid-mapping
    // perimeters already make).
    //
    // KNOWN CAVEAT: unlike the old per-fragment connector, this doesn't know
    // about real water bodies - if the traced corridor's shortest path
    // crosses a lake/reservoir, the fill will cover it too (no hole cut out).
    // Revisit with a server-side "subtract real water geometry from this
    // hull" step (reusing services/geo_filter.py's water-tile fetch, already
    // proven via /api/geo/segment-crosses-water) if that turns out to matter
    // in practice.
    const points = group.map((f) => [f.latitude, f.longitude]);
    const rawHull = concaveHull(points);
    if (!rawHull || !isHullReasonablyCompact(rawHull)) return;
    const hull = smoothRing(rawHull);
    // Too small a real-world area to be a meaningful shape - see
    // MIN_HULL_AREA_HA. The group's own points still render as plain dots
    // regardless (see visiblePointFires below), just without a polygon.
    if (ringAreaHectares(hull) < MIN_HULL_AREA_HA) return;

    // A thin dark outline (POLYGON_OUTLINE) reads fine against the light
    // basemap tiles, but gets visually lost once dot markers sit on top of it -
    // the polygon's extent stopped being readable at a glance. A bold white
    // halo underneath a solid, fairly thick dark line gives a contour that
    // reads against any basemap color AND against the dots sitting on top
    // of it.
    const haloWeight = 5.5;
    const strokeWeight = 2.5;

    // Draws the halo + filled polygon for a given ring/multi-ring shape, and
    // returns both layers so the caller can remove them again - shared by
    // the initial plain-hull render below AND by the water-subtracted
    // replacement (see waterSubtractedHullLatLngs above), so both paths get
    // IDENTICAL styling with no drift between them.
    const drawHullShape = (latlngsForLeaflet) => {
      const shapeHalo = L.polygon(latlngsForLeaflet, {
        pane: "hullPane",
        color: "#ffffff",
        weight: haloWeight,
        opacity: 0.9,
        fill: false,
      });
      shapeHalo.addTo(markersLayer);

      // Flat neutral gray, not a color-coded (e.g. recency) fill - every raw
      // detection dot already renders its OWN recency color on top of this
      // fill at every zoom (see the visiblePointFires rendering pass below),
      // so a second color-coded fill underneath just muddies the two
      // signals together (confirmed live: a red-to-blue recency gradient
      // fill mixed with the dots' own red/orange/yellow into an
      // unreadable purple wash over a dense cluster). A plain gray still
      // communicates "this is the fire's extent" without competing with the
      // dots for the same color channel - matching how Copernicus's own
      // EMSR grading maps use a flat, muted burnt-area fill with small vivid
      // point markers on top, rather than color-coding the fill itself.
      const shapePolygon = L.polygon(latlngsForLeaflet, {
        pane: "hullPane",
        color: POLYGON_OUTLINE,
        weight: strokeWeight,
        fillColor: "#8a8577",
        fillOpacity: 0.14,
      });
      attachGeocode(shapePolygon, group, earliest, mostRecent, matchedIncident, growth);
      shapePolygon.addTo(markersLayer);
      return { shapeHalo, shapePolygon };
    };

    let { shapeHalo, shapePolygon } = drawHullShape(hull);

    // Fire-and-forget: the plain hull above is already on the map (nothing
    // waits on this network round-trip), and if a real lake/reservoir turns
    // out to sit inside it, swap in the water-subtracted version in place.
    // Any failure (see waterSubtractedHullLatLngs) simply leaves the plain
    // hull rendered - never a broken or missing shape.
    waterSubtractedHullLatLngs(hull).then((cutLatLngs) => {
      if (!cutLatLngs) return;
      markersLayer.removeLayer(shapeHalo);
      markersLayer.removeLayer(shapePolygon);
      ({ shapeHalo, shapePolygon } = drawHullShape(cutLatLngs));
    });
  });

  // Detections belonging to a polygon hidden by the sidebar filters (above)
  // shouldn't reappear as loose clustered/isolated dots either.
  const visiblePointFires = pointFires.filter((f) => !filteredOutPoints.has(f));

  // Plot every raw detection as its own small fixed-radius dot, at every
  // zoom level - shows the actual hotspot density/shape texture (matches how
  // other fire-monitoring maps, e.g. Pyrofire, render FIRMS/VIIRS data:
  // hundreds of individual colored dots forming a directional "comet tail",
  // not a handful of merged blobs). Each dot keeps its own recency color
  // rather than a cluster-averaged one, so the color gradient across dots
  // reads as spread direction over time - including at low zoom, where this
  // used to be lost to grid-cell decimation (see the note above
  // groupFiresByProximity). Rendered on the shared canvas renderer
  // (hotspotRenderer) rather than Leaflet's default SVG so plotting every
  // point - even the full-Spain, 30-day case (~11.5k detections measured
  // live) - stays smooth instead of choking on thousands of DOM nodes.
  visiblePointFires.forEach((fire) => {
    const marker = hotspotMarker(fire, {
      radius: HOTSPOT_DOT_RADIUS,
      ringColor: DOT_RING_COLOR,
      ringWeight: 1,
      renderer: hotspotRenderer,
    });
    // A dot whose fire belongs to a matched incident gets the SAME rich,
    // incident-linked popup (name, timeline button, growth, telegram/satellite
    // sections) the polygon gets via attachGeocode - not just a bare
    // "Detectado {date}" line with no way back to the sidebar/timeline. This
    // matters even when a polygon DOES render for the incident (clicking a
    // dot instead of the shape underneath it still surfaces the same info),
    // and matters MOST when no polygon rendered at all (compactness/area
    // gates above, or fewer than 3 points) - previously that always meant a
    // complete dead end back to the incident, regardless of how well-known
    // or large the fire actually was.
    const info = incidentInfoByFire.get(fire);
    if (info) {
      attachGeocode(marker, info.group, info.earliest, info.mostRecent, info.matchedIncident, info.growth);
    } else {
      marker.bindPopup(`<div class="card-meta">Detectado &nbsp;${fire.acquired_at}</div>`);
    }
    marker.addTo(markersLayer);
  });

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
// A manually-set official_name (see PATCH /api/incidents/{id}, set from the
// ranking page's rename control) always wins over the reverse-geocoded
// locality wherever an incident's name is displayed - same rule ranking.js's
// own displayName() already applies to the ranking table/report tab.
function displayName(incident) {
  return (incident && (incident.official_name || incident.locality)) || `Foco sin nombre #${incident.id}`;
}

function incidentAreaHa(incident) {
  const growth = incidentEstimatesById.get(incident.id) || null;
  if (incident.area_ha != null) return { areaHa: incident.area_ha, isOfficial: true };
  if (growth && growth.areaHa >= 0.1) return { areaHa: growth.areaHa, isOfficial: false };
  return null;
}

function incidentCardHtml(incident) {
  const name = displayName(incident);
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
    const haystack = normalizeSearchText(`${incident.official_name || ""} ${incident.locality || ""} ${incident.province || ""}`);
    if (!haystack.includes(filters.searchText)) return false;
  }
  return true;
}

function applyFilters(incidents) {
  const filters = getActiveFilters();
  // Detection count, highest first - a more concrete, at-a-glance read of
  // "how much satellite activity is really behind this fire" than the
  // backend's own severity_score ordering (which also folds in area/
  // duration and can put a small-but-old smoldering fire above a fresh,
  // fast-growing one with far more actual detections).
  return incidents.filter((incident) => incidentPassesFilters(incident, filters)).sort((a, b) => b.detection_count - a.detection_count);
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

// incident.first_detected_at can be far older than when this fire actually
// ignited: merge_reassociable_incidents (backend/app/services/incidents.py)
// folds an old, small, unrelated detection cluster into a real fire's record
// whenever their centroids happen to fall within reach of each other, and
// first_detected_at is then the min() across that merge - see the identical
// problem/fix already applied to the daily chart in dailyDetectionCounts'
// onset-detection comment above. Reuses that exact same onset day so "Tiempo
// activo" and the chart right below it never quote two different starts for
// the same incident. Returns null (no correction) if there's no detection
// history to derive an onset from, or if the incident's very first tracked
// day already IS its real onset (nothing to correct).
function effectiveFirstDetectedAt(events, rawFirstDetectedAt) {
  const series = dailyDetectionCounts(events);
  const onsetDay = series.find((d) => d.count > 0)?.day;
  if (!onsetDay) return null;
  const onsetIso = `${onsetDay}T00:00:00Z`;
  return onsetIso > rawFirstDetectedAt ? onsetIso : null;
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

// A Copernicus EMS activation can span several AOIs, each with its own
// lazily-rendered satellite preview (GET
// /api/copernicus-ems/products/{id}/thumbnail - see
// services/copernicus_ems_imagery.py) - so unlike the single-image sources
// above, this returns a list rather than one URL.
function emsProductImageUrls(event) {
  if (!event.raw_data) return [];
  try {
    const data = JSON.parse(event.raw_data);
    if (Array.isArray(data.ems_product_ids)) {
      return data.ems_product_ids.map((id) => `${apiBaseUrl}/api/copernicus-ems/products/${id}/thumbnail`);
    }
  } catch {
    return [];
  }
  return [];
}

// event.description is server-generated free text (analyst summaries,
// impact stats, a report URL) - escaped first so it's never interpreted as
// HTML, then any bare http(s) URL (e.g. the EMS "Mapa oficial: ..."
// StoryMap link) is turned into a real clickable link.
function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function linkifyDescription(text) {
  if (!text) return "";
  return escapeHtml(text).replace(
    /(https?:\/\/[^\s]+)/g,
    (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
  );
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
  ems_activation: ICONS.badge,
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
  const emsImages = event.event_type === "ems_activation" ? emsProductImageUrls(event) : [];
  const icon = EVENT_TYPE_ICON[event.event_type] || "";
  return (
    `<div class="timeline-item">` +
    `<span class="timeline-dot timeline-dot-${event.event_type || "default"}">${icon}</span>` +
    `<div class="timeline-time">${event.occurred_at}</div>` +
    `<div class="timeline-title">${event.title}</div>` +
    (event.description ? `<div class="timeline-desc">${linkifyDescription(event.description)}</div>` : "") +
    (imageUrl ? `<img src="${imageUrl}" class="timeline-thumb" />` : "") +
    (emsImages.length
      ? `<div class="ems-aoi-thumbs">` +
        emsImages.map((url) => `<img src="${url}" class="timeline-thumb ems-aoi-thumb" loading="lazy" />`).join("") +
        `</div>`
      : "") +
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

const DETECTION_SOURCE_LABELS = { FIRMS: "NASA FIRMS", EFFIS: "EFFIS", EUMETSAT: "EUMETSAT", SENTINEL3: "Sentinel-3" };

// Fetches this incident's FULL, unfiltered detection set (see
// selectedIncidentDetections) and uses it for two things: letting the map
// always show every one of this fire's points regardless of the date-range
// filter/scrubber, and answering "when did we last actually hear from
// FIRMS/EUMETSAT/etc. about THIS fire" - a per-source freshness line the
// global filter can't answer since it only knows about whatever's currently
// loaded map-wide.
async function loadIncidentDetections(incident) {
  try {
    const res = await fetch(`${apiBaseUrl}/api/incidents/${incident.id}/detections`);
    const detections = await res.json();
    // Stale guard: the user may have gone back to the list, or opened a
    // different incident, before this network round-trip resolved.
    if (predictionIncident !== incident) return;
    selectedIncidentDetections = detections;

    const lastBySource = new Map();
    detections.forEach((d) => {
      const prev = lastBySource.get(d.source);
      if (!prev || new Date(d.acquired_at) > new Date(prev)) lastBySource.set(d.source, d.acquired_at);
    });
    const slot = document.getElementById("source-freshness-slot");
    if (slot) {
      slot.innerHTML = lastBySource.size
        ? `<div class="source-freshness">` +
          Array.from(lastBySource.entries())
            .map(([source, at]) => `<span>${DETECTION_SOURCE_LABELS[source] || source}: ${relativeTime(at)}</span>`)
            .join("") +
          `</div>`
        : "";
    }

    if (!detections.length) {
      renderMap();
      return;
    }
    // The scrubber's start bound/label described the map's global
    // date-range window (e.g. "last 14 days"), not this fire - now that its
    // own full history is loaded, describe THAT instead. Re-syncing the
    // range's value to the (possibly shifted) "Ahora" fraction, then
    // re-running onScrubberInput, keeps it consistent with whatever
    // enableIncidentPrediction already set it to against the OLD bounds
    // (and re-renders the map in the process, so no separate call needed).
    scrubberBounds = { startMs: new Date(detections[0].acquired_at).getTime(), endMs: Date.now() };
    document.getElementById("scrubber-label-start").textContent = new Date(
      scrubberBounds.startMs
    ).toLocaleString("es-ES", SCRUBBER_DATE_FORMAT);
    document.getElementById("scrubber-range").value = scrubberNowFraction();
    onScrubberInput();
  } catch {
    // Best-effort - the detail view already has plenty to show without this.
  }
}

async function showIncidentDetail(incident) {
  map.flyTo([incident.centroid_lat, incident.centroid_lon], Math.max(map.getZoom(), 11));

  // Filters apply to the list, not to a single incident's detail - hide
  // them to give the detail card the space instead of leaving them shown
  // above it for no reason. Restored (below) when going back to the list.
  document.getElementById("filter-bar").classList.add("filter-bar-hidden");
  // Same reasoning for the top summary bar - its counts are a NATIONAL
  // overview, unrelated to (and confusable with) the one incident now on
  // screen.
  document.getElementById("summary-bar").classList.add("summary-bar-hidden");

  document.getElementById("sidebar-header").innerHTML =
    `<button class="sidebar-back" id="sidebar-back" title="Volver a la lista">&larr;</button><h2>Detalle del incidente</h2>`;
  document.getElementById("sidebar-back").addEventListener("click", () => {
    document.getElementById("filter-bar").classList.remove("filter-bar-hidden");
    document.getElementById("summary-bar").classList.remove("summary-bar-hidden");
    disableIncidentPrediction();
    selectedIncidentDetections = [];
    initTimelineScrubber(lastFires); // restores the normal date-range-filtered bounds/label
    refreshIncidentList();
  });

  // Extends the bottom scrubber past "Ahora" with this incident's own wind
  // forecast (same model/endpoint the standalone "place origin" tool uses,
  // just aimed at the incident's centroid automatically) - see
  // enableIncidentPrediction. Fire-and-forget: the detail card itself
  // doesn't wait on this network round-trip.
  enableIncidentPrediction(incident);

  const name = displayName(incident);
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
    `<div class="incident-metric" id="incident-duration-metric"><div class="incident-metric-value">${durationLabel(incident.first_detected_at, incident.last_detected_at)}</div><div class="incident-metric-label">Tiempo activo</div></div>` +
    `<div class="incident-metric" id="incident-area-metric">` +
    (areaHa != null
      ? `<div class="incident-metric-value">${Math.round(areaHa).toLocaleString()}</div><div class="incident-metric-label">Hectáreas${hasOfficialArea ? "" : " (estimado)"}</div>`
      : `<div class="incident-metric-value" style="font-size:15px;">${relativeTime(incident.last_detected_at)}</div><div class="incident-metric-label">Última actualización</div>`) +
    `</div>` +
    `</div>` +
    `<div id="source-freshness-slot"></div>` +
    // Placeholder - filled in once the full-history timeline loads below.
    // The old version rendered this synchronously from `growth.timestamps`
    // (only whatever's currently loaded on the map under the active
    // date-range filter), which is why a 10-day-old incident's chart looked
    // almost empty when the map was showing "last 48h" - this now always
    // reflects the incident's REAL full history regardless of that filter.
    `<div id="daily-chart-slot"></div>` +
    `</div>` +
    // Ordered by what a stressed-out user needs most: active personnel/
    // aircraft first (accent-highlighted, see .priority-card), then the
    // visual "what does it look like" satellite read, then the lower-signal
    // Telegram mentions, with the full event-by-event log collapsed at the
    // very bottom since it's supporting detail, not the primary read.
    `<div id="regional-status-slot"></div>` +
    `<div id="satellite-carousel-slot"></div>` +
    `<div id="vegetation-slot"></div>` +
    `<div id="weather-slot"></div>` +
    `<div id="telegram-section-slot"></div>` +
    `<button class="timeline-toggle" id="timeline-toggle">` +
    `<span>Ver cronología</span><span class="timeline-toggle-chevron">▾</span>` +
    `</button>` +
    `<div class="timeline-list" id="timeline-list"><div class="sidebar-empty">Cargando cronología…</div></div>` +
    // Ranking's own report view for this incident already has strictly more
    // depth than this panel (source breakdown, a real map, personnel grid,
    // full satellite/Telegram/timeline) - rather than growing this sidebar
    // into a third copy of the same thing, it's the one deep-dive
    // destination every "tell me everything" path leads to.
    `<div class="full-info-row"><a class="full-info-btn" href="/ranking.html#/incident/${incident.id}" target="_blank" rel="noopener">Información completa &rarr;</a></div>`;

  document.getElementById("timeline-toggle").addEventListener("click", () => {
    document.getElementById("timeline-toggle").classList.toggle("expanded");
    document.getElementById("timeline-list").classList.toggle("expanded");
  });

  // Fire-and-forget, same pattern as the map popup's async sections - each
  // slot fills in independently as its own fetch resolves, instead of
  // blocking the whole detail card on the slowest of the three.
  loadIncidentDetections(incident);
  getRegionalStatus(incident.id).then((records) => {
    const html = regionalSectionHtml(records);
    document.getElementById("regional-status-slot").innerHTML = html ? `<div class="priority-card">${html}</div>` : "";
  });
  getTelegramMentions(incident.id).then((messages) => {
    document.getElementById("telegram-section-slot").innerHTML = telegramSectionHtml(messages);
  });
  // Skip the round-trip entirely for the vast majority of incidents that
  // never had a Copernicus EMS activation - has_ems_activation is already
  // known from the incident list, no need to ask just to get null back.
  if (incident.has_ems_activation) {
    getVegetationStats(incident.id).then((stats) => {
      document.getElementById("vegetation-slot").innerHTML = vegetationSectionHtml(stats);
    });
  }

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

    // Correct "Tiempo activo" to the same real-onset day the chart above
    // already trims to, so the two never disagree about when this fire
    // actually started (see effectiveFirstDetectedAt).
    const correctedStart = effectiveFirstDetectedAt(events, incident.first_detected_at);
    if (correctedStart) {
      const metric = document.getElementById("incident-duration-metric");
      metric.querySelector(".incident-metric-value").textContent = durationLabel(correctedStart, incident.last_detected_at);
      metric.querySelector(".incident-metric-label").textContent = "Tiempo activo (real)";
      metric.title = "Corregido: la fecha de primera detección incluye una fusión con una detección antigua no relacionada.";
    }

    const list = document.getElementById("timeline-list");
    list.innerHTML = events.length
      ? groupTimelineEvents(events).map(timelineItemHtml).join("")
      : `<div class="sidebar-empty">Sin eventos registrados para este incidente.</div>`;
    document.querySelector("#timeline-toggle span").textContent = `Ver cronología (${events.length})`;
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

async function loadIncidents() {
  try {
    // Same hours window as the map's date-range filter (getSelectedDays), so
    // the sidebar list, the map, and any open timeline all agree on "what's
    // in scope right now" instead of the sidebar always showing 30 days.
    const hours = getSelectedDays() * 24;
    const res = await fetch(`${apiBaseUrl}/api/incidents?sort=severity&hours=${hours}`);
    lastIncidents = await res.json();
    renderIncidentList(applyFilters(lastIncidents));
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
    initTimelineScrubber(lastFires);
    renderMap();
    // Fire-and-forget - wind vectors + the summary bar's wind figure depend
    // on a handful of extra network round-trips (one per active incident)
    // and shouldn't block the map's first paint.
    refreshWindVectors(lastIncidents);
    updateSatelliteFreshness();
  } catch (err) {
    setStatus(`No se pudieron cargar los datos: ${err.message}`);
  }
}

// ---------- Wind vectors (per active incident, distinct from severity) ----------
// A small rotated arrow at each active incident's centroid shows current
// wind speed/direction, reusing the same Open-Meteo-backed endpoint the
// experimental fire-spread tool already calls for its 24h forecast - here
// capped to 1h since this only needs "right now". Capped to a handful of
// incidents at once so a busy day doesn't fire dozens of concurrent
// requests against Open-Meteo just to paint arrows.
const MAX_WIND_VECTORS = 20;
const windCacheByIncidentId = new Map(); // id -> { speedKmh, directionFromDeg } | null (fetch failed)

function windArrowIcon(directionFromDeg, speedKmh) {
  // Arrow points where the wind is blowing TOWARD (180deg from the "from"
  // direction the API returns) - i.e. the direction the fire is being
  // pushed, matching how the fire-spread tool's own hourly ellipses orient.
  const rotation = (directionFromDeg + 180) % 360;
  const opacity = Math.min(1, 0.55 + speedKmh / 60).toFixed(2);
  return L.divIcon({
    className: "wind-arrow-icon",
    html:
      `<div class="wind-arrow" style="transform:rotate(${rotation}deg); opacity:${opacity};">` +
      `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="var(--wind)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V3M12 3l-6 6M12 3l6 6"/></svg>` +
      `</div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

async function fetchWindForIncident(incident) {
  if (windCacheByIncidentId.has(incident.id)) return windCacheByIncidentId.get(incident.id);
  try {
    const res = await fetch(
      `${apiBaseUrl}/api/fire-spread/predict?lat=${incident.centroid_lat}&lon=${incident.centroid_lon}&max_hours=1`
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const first = data.hourly && data.hourly[0];
    const wind = first ? { speedKmh: first.wind_speed_kmh, directionFromDeg: first.wind_direction_from_deg } : null;
    windCacheByIncidentId.set(incident.id, wind);
    return wind;
  } catch {
    windCacheByIncidentId.set(incident.id, null);
    return null;
  }
}

async function refreshWindVectors(incidents) {
  const active = incidents.filter((incident) => incident.status === "active").slice(0, MAX_WIND_VECTORS);
  const winds = await Promise.all(active.map(fetchWindForIncident));
  windLayer.clearLayers();
  active.forEach((incident, idx) => {
    const wind = winds[idx];
    if (!wind) return;
    L.marker([incident.centroid_lat, incident.centroid_lon], {
      icon: windArrowIcon(wind.directionFromDeg, wind.speedKmh),
      interactive: true,
      zIndexOffset: 500,
    })
      .bindTooltip(`${Math.round(wind.speedKmh)} km/h desde el ${compassLabel(wind.directionFromDeg)}`, {
        className: "wind-arrow-tooltip",
      })
      .addTo(windLayer);
  });
  updateSummaryBar(incidents, winds.filter(Boolean));
}

// ---------- National summary bar ----------
function riskRank(riskLevel) {
  return { low: 0, moderate: 1, high: 2, critical: 3 }[riskLevel] ?? -1;
}

function updateSummaryBar(incidents, resolvedWinds) {
  const active = incidents.filter((incident) => incident.status === "active");
  document.getElementById("summary-active").textContent = active.length;

  const totalHa = active.reduce((sum, incident) => {
    const area = incidentAreaHa(incident);
    return sum + (area ? area.areaHa : 0);
  }, 0);
  document.getElementById("summary-area").textContent = totalHa > 0 ? Math.round(totalHa).toLocaleString() : "–";

  const maxRisk = active.reduce(
    (best, incident) => (riskRank(incident.risk_level) > riskRank(best) ? incident.risk_level : best),
    "low"
  );
  const riskEl = document.getElementById("summary-risk");
  riskEl.textContent = active.length ? RISK_LABELS[maxRisk] || maxRisk : "–";
  riskEl.className = `summary-stat-value summary-risk${active.length ? ` risk-text-${maxRisk}` : ""}`;

  const windEl = document.getElementById("summary-wind");
  if (resolvedWinds && resolvedWinds.length) {
    const avgSpeed = resolvedWinds.reduce((sum, wind) => sum + wind.speedKmh, 0) / resolvedWinds.length;
    windEl.textContent = `${Math.round(avgSpeed)} km/h`;
  } else {
    windEl.textContent = "–";
  }
}

// Most recent time any satellite source (FIRMS/EFFIS/EUMETSAT/Sentinel-3)
// actually wrote genuinely NEW data - see GET /api/sources' last_data_at
// (routers/sources.py), which distinguishes "checked" from "found something
// new" the same way this bar's other stats reflect real fire state, not just
// "the backend polled recently".
async function updateSatelliteFreshness() {
  const el = document.getElementById("summary-satellite");
  try {
    const res = await fetch(`${apiBaseUrl}/api/sources`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const sources = await res.json();
    const latest = sources
      .filter((s) => s.category === "satellite" && s.last_data_at)
      .map((s) => new Date(s.last_data_at).getTime())
      .reduce((max, t) => Math.max(max, t), 0);
    el.textContent = latest ? relativeTime(new Date(latest).toISOString()) : "–";
  } catch {
    el.textContent = "–";
  }
}

// ---------- Bottom timeline scrubber ----------
// Drag (or hit play) to replay the currently-loaded window's detections up
// to a given moment, so a fire's spread over the day becomes watchable
// instead of a single static snapshot. Filters lastFires client-side and
// re-renders - never re-fetches, so scrubbing stays instant.
//
// When an incident's detail view is open, the SAME control extends past
// "Ahora" with that incident's own wind-driven spread forecast (see
// enableIncidentPrediction below) - dragging into that zone doesn't filter
// detections (the map keeps showing the current, unfiltered state) but
// instead draws the predicted ellipse for the corresponding hour and
// projects the incident's hectares metric forward. "Ahora" stops being
// pinned to the right edge in that mode; it sits wherever the past/future
// split falls (scrubberNowFraction), and the reset button always returns
// to it regardless of where that is.
const SCRUBBER_DATE_FORMAT = { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" };
let scrubberBounds = null; // { startMs, endMs } spanning the currently loaded historical window
let scrubberPlayTimer = null;
let scrubberFutureMs = 0; // 0 unless a prediction is active (see enableIncidentPrediction)
let scrubberFutureHours = 0;
let predictionIncident = null; // the incident the active forecast belongs to, or null
// This incident's own detections (see /api/incidents/{id}/detections), fetched with NO
// hours/date-range restriction - unlike lastFires. Non-empty only while an incident's
// detail view is open. Lets that incident always show its FULL history on the map
// regardless of the date-range filter/scrubber position (see scrubberFilteredFires and
// showIncidentDetail) - the thing being viewed shouldn't disappear just because the
// global filter/scrubber has moved past its own detections.
let selectedIncidentDetections = [];

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Two independent features can each extend the scrubber past "Ahora" - a
// single incident's fire-spread prediction (scrubberFutureHours/Ms) and the
// viewport-wide wind field (windFieldFutureHours/Ms). Neither knows about
// the other, so the scrubber itself always uses whichever extends further -
// they're never meaningfully different in practice (both request a 24h
// Open-Meteo window), but this keeps either one alone, or both together,
// correct without special-casing which one is active.
function effectiveFutureHours() {
  return Math.max(scrubberFutureHours, windFieldFutureHours);
}
function effectiveFutureMs() {
  return Math.max(scrubberFutureMs, windFieldFutureMs);
}

// Where "Ahora" falls on the 0-100 track - 100 (the right edge) with no
// prediction active, or an interior point once the future span stretches
// the track's total span past "now".
function scrubberNowFraction() {
  if (!scrubberBounds) return 100;
  const pastSpan = scrubberBounds.endMs - scrubberBounds.startMs;
  const total = pastSpan + effectiveFutureMs();
  return total > 0 ? (pastSpan / total) * 100 : 100;
}

function updateScrubberEndLabel() {
  const hours = effectiveFutureHours();
  document.getElementById("scrubber-label-end").textContent = hours > 0 ? `+${hours}h` : "Ahora";
}

function initTimelineScrubber(fires) {
  disableIncidentPrediction(); // a fresh full data load always drops any per-incident forecast in progress
  stopScrubberPlayback();
  const range = document.getElementById("scrubber-range");
  const startLabel = document.getElementById("scrubber-label-start");
  range.value = 100;
  document.getElementById("scrubber-label-current").textContent = "";
  if (!fires.length) {
    scrubberBounds = null;
    startLabel.textContent = "–";
    updateScrubberEndLabel();
    return;
  }
  // Reduce, not Math.min/max(...times) - this map already deals with 10k+
  // detections on a wide date range, well past the argument-count ceiling
  // spreading an array into Math.min/max would hit on some engines.
  let startMs = Infinity;
  let endMs = Date.now();
  fires.forEach((fire) => {
    const ms = new Date(fire.acquired_at).getTime();
    if (ms < startMs) startMs = ms;
    if (ms > endMs) endMs = ms;
  });
  scrubberBounds = { startMs, endMs };
  startLabel.textContent = new Date(startMs).toLocaleString("es-ES", SCRUBBER_DATE_FORMAT);
  updateScrubberEndLabel();
}

// Used by renderMap() in place of the raw lastFires array - returns
// everything at "Ahora" or later (including anywhere in the predicted
// future zone, which doesn't filter detections at all), otherwise only
// detections at or before the scrubbed instant.
function scrubberFilteredFires(allFires) {
  const range = document.getElementById("scrubber-range");
  if (!scrubberBounds) return allFires;
  const nowFraction = scrubberNowFraction();
  const value = Number(range.value);
  if (value >= nowFraction) return allFires;
  const pastSpan = scrubberBounds.endMs - scrubberBounds.startMs;
  const fraction = nowFraction > 0 ? value / nowFraction : 1;
  const cutoffMs = scrubberBounds.startMs + pastSpan * Math.min(1, Math.max(0, fraction));
  return allFires.filter((fire) => new Date(fire.acquired_at).getTime() <= cutoffMs);
}

function onScrubberInput() {
  const range = document.getElementById("scrubber-range");
  const currentLabel = document.getElementById("scrubber-label-current");
  const value = Number(range.value);

  currentLabel.classList.remove("future");

  if (!scrubberBounds) {
    currentLabel.textContent = "";
    renderMap();
    return;
  }

  const nowFraction = scrubberNowFraction();
  if (value < nowFraction) {
    // Past: filter detections up to the scrubbed instant, same behavior as
    // before prediction existed.
    clearPredictionEllipse();
    clearWindField();
    const pastSpan = scrubberBounds.endMs - scrubberBounds.startMs;
    const fraction = nowFraction > 0 ? value / nowFraction : 1;
    const cutoffMs = scrubberBounds.startMs + pastSpan * Math.min(1, Math.max(0, fraction));
    currentLabel.textContent = new Date(cutoffMs).toLocaleString("es-ES", SCRUBBER_DATE_FORMAT);
    renderMap();
    return;
  }

  if (value <= nowFraction + 0.01 || effectiveFutureHours() === 0) {
    // Sitting exactly at "Ahora" (or no forecast to show past it).
    currentLabel.textContent = "";
    clearPredictionEllipse();
    if (windFieldData) renderWindFieldAtHour(1);
    else clearWindField();
    renderMap();
    return;
  }

  // Future: base map stays at its unfiltered "now" state; overlay the
  // predicted spread ellipse and/or wind field for the corresponding
  // forecast hour instead.
  renderMap();
  const futureHours = effectiveFutureHours();
  const futureFraction = (value - nowFraction) / (100 - nowFraction || 1);
  const hoursAhead = Math.min(futureHours, Math.max(1, Math.round(futureFraction * futureHours)));
  currentLabel.classList.add("future");
  currentLabel.textContent = `+${hoursAhead}h (previsto)`;
  applyPredictionEllipse(hoursAhead);
  renderWindFieldAtHour(hoursAhead);
}

function stopScrubberPlayback() {
  if (scrubberPlayTimer) {
    clearInterval(scrubberPlayTimer);
    scrubberPlayTimer = null;
  }
  document.getElementById("scrubber-play").classList.remove("is-playing");
  document.getElementById("scrubber-play-icon").innerHTML = `<path d="M6 4l14 8-14 8z"/>`;
}

function toggleScrubberPlayback() {
  if (scrubberPlayTimer) {
    stopScrubberPlayback();
    return;
  }
  if (!scrubberBounds) return;
  const range = document.getElementById("scrubber-range");
  if (Number(range.value) >= 100) range.value = 0; // replaying from the end restarts from the beginning
  document.getElementById("scrubber-play").classList.add("is-playing");
  document.getElementById("scrubber-play-icon").innerHTML =
    `<rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/>`;
  scrubberPlayTimer = setInterval(() => {
    const next = Math.min(100, Number(range.value) + 1.2);
    range.value = next;
    onScrubberInput();
    if (next >= 100) stopScrubberPlayback();
  }, 90);
}

document.getElementById("scrubber-range").addEventListener("input", onScrubberInput);
document.getElementById("scrubber-play").addEventListener("click", toggleScrubberPlayback);
document.getElementById("scrubber-reset").addEventListener("click", () => {
  stopScrubberPlayback();
  document.getElementById("scrubber-range").value = scrubberNowFraction();
  onScrubberInput();
});

// ---------- Scrubber <-> fire-spread prediction bridge ----------
// Opening an incident's detail view auto-runs a multi-front wind-forecast
// prediction FROM the fire's own recent leading edge(s) (see
// /api/fire-spread/predict-incident and predict_incident_spread in the
// backend) - unlike the standalone "place origin" experimental tool below
// (a single manually-clicked point, its own separate fireSpreadData/slider),
// this one starts from wherever the incident has actually been active
// lately, can draw more than one ellipse when the fire has more than one
// active flank, and won't grow an ellipse backward into ground the fire
// already burnt. One scrubber control for both "what happened" and "where
// this might go next".
let incidentFireSpreadData = null; // { fronts: [{ hourly: [...] }, ...], burnt_area_hull } | null

async function enableIncidentPrediction(incident) {
  predictionIncident = incident;
  fireSpreadOriginLayer.clearLayers();
  incidentFireSpreadData = null;
  scrubberFutureMs = 0;
  scrubberFutureHours = 0;
  updateScrubberEndLabel();
  try {
    const res = await fetch(`${apiBaseUrl}/api/fire-spread/predict-incident/${incident.id}?max_hours=8`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    // Stale guard: the user may have gone back to the list, or opened a
    // different incident, before this network round-trip resolved.
    if (predictionIncident !== incident) return;
    incidentFireSpreadData = data;
    const firstFront = data.fronts[0];
    const weatherSlot = document.getElementById("weather-slot");
    if (weatherSlot && firstFront) weatherSlot.innerHTML = weatherSectionHtml(firstFront.hourly[0], firstFront.air_quality);
    scrubberFutureHours = firstFront ? firstFront.hourly.length : 0;
    scrubberFutureMs = scrubberFutureHours * 3600000;
    updateScrubberEndLabel();
    // Land on "Ahora" by default - the user drags right to see the
    // forecast, rather than every incident click jumping straight to the end.
    document.getElementById("scrubber-range").value = scrubberNowFraction();
    onScrubberInput();
  } catch {
    // Wind/elevation lookups can fail (Open-Meteo hiccup, missing slope
    // data, etc.), or a very quiet/tiny incident may have too few recent
    // detections to trace a front from - leave the scrubber as a plain
    // historical control rather than surfacing an error for what's
    // presented as a bonus on top of the detail view, not its primary purpose.
  }
}

function disableIncidentPrediction() {
  predictionIncident = null;
  incidentFireSpreadData = null;
  scrubberFutureMs = 0;
  scrubberFutureHours = 0;
  fireSpreadLayer.clearLayers();
  restoreAreaMetric();
  updateScrubberEndLabel();
}

function clearPredictionEllipse() {
  fireSpreadLayer.clearLayers();
  restoreAreaMetric();
}

// Swaps the incident detail card's hectares metric for a live "current +
// predicted growth" figure while scrubbing through the forecast - reverted
// (restoreAreaMetric) the moment the scrubber leaves the future zone.
let originalAreaMetricHtml = null;

function applyPredictionEllipse(hoursAhead) {
  if (!incidentFireSpreadData) return;
  const windColor = cssVar("--wind") || "#0c7f8c";
  fireSpreadLayer.clearLayers();
  // Summed across every active front - a fire with two flanks predicts (and
  // projects hectares for) both at once, not just whichever one happened to
  // be picked as "the" origin. Fronts are clustered far enough apart that
  // double-counting overlap is a non-issue in practice (see FRONT_CLUSTER_DEG).
  let totalEllipseHa = 0;
  let representativeHourEntry = null;
  incidentFireSpreadData.fronts.forEach((front) => {
    const hourEntry = front.hourly[hoursAhead - 1] || front.hourly[front.hourly.length - 1];
    if (!hourEntry) return;
    representativeHourEntry = representativeHourEntry || hourEntry;
    totalEllipseHa += ringAreaHectares(hourEntry.polygon);
    L.polygon(hourEntry.polygon, {
      color: windColor,
      weight: 2,
      dashArray: "5 4",
      fillColor: windColor,
      fillOpacity: 0.16,
    })
      .bindTooltip(
        `Previsión +${hourEntry.hour}h · ${hourEntry.wind_speed_kmh} km/h desde el ${compassLabel(hourEntry.wind_direction_from_deg)}`,
        { sticky: true }
      )
      .addTo(fireSpreadLayer);
  });
  if (!representativeHourEntry) return;

  const areaMetricEl = document.getElementById("incident-area-metric");
  if (!areaMetricEl || !predictionIncident) return;
  if (originalAreaMetricHtml === null) originalAreaMetricHtml = areaMetricEl.innerHTML;
  const baseAreaHa = incidentAreaHa(predictionIncident);
  const projectedHa = (baseAreaHa ? baseAreaHa.areaHa : 0) + totalEllipseHa;
  areaMetricEl.innerHTML =
    `<div class="incident-metric-value" style="color:var(--wind);">${Math.round(projectedHa).toLocaleString()}</div>` +
    `<div class="incident-metric-label">Ha previstas (+${representativeHourEntry.hour}h)</div>`;
}

function restoreAreaMetric() {
  const areaMetricEl = document.getElementById("incident-area-metric");
  if (areaMetricEl && originalAreaMetricHtml !== null) {
    areaMetricEl.innerHTML = originalAreaMetricHtml;
  }
  originalAreaMetricHtml = null;
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

// ---------- ADS-B aircraft (adsb.lol) ----------
// Live per-viewport layer, same shape as the webcams layer above - never
// persisted, refetched on toggle-on and on every pan/zoom (see the
// map.on("moveend", ...) wiring near the bottom of this file).
function aircraftIcon(trackDeg) {
  const rotation = trackDeg != null ? trackDeg : 0;
  return L.divIcon({
    className: "",
    html:
      `<div style="transform:rotate(${rotation}deg);">` +
      `<svg viewBox="0 0 24 24" width="16" height="16" fill="#0ea5e9" stroke="#1e293b" stroke-width="0.8"><path d="M12 2l2 6 7 3v2l-7-1v5l3 2v2l-5-1.5L7 22v-2l3-2v-5l-7 1v-2l7-3z"/></svg>` +
      `</div>`,
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function aircraftPopupHtml(ac) {
  const parts = [
    ac.flight ? `<div class="card-title">✈️ ${ac.flight}</div>` : `<div class="card-title">✈️ ${ac.hex || "Aeronave"}</div>`,
    `<div class="card-meta">` +
      [
        ac.aircraft_type,
        ac.altitude_ft != null ? `${Math.round(ac.altitude_ft).toLocaleString()} ft` : null,
        ac.ground_speed_kt != null ? `${Math.round(ac.ground_speed_kt)} kt` : null,
      ]
        .filter(Boolean)
        .join(" · ") +
      `</div>`,
  ];
  return parts.join("");
}

async function reloadAircraft() {
  if (!document.getElementById("aircraft-toggle").checked) return;
  const bounds = map.getBounds();
  const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(",");
  try {
    const res = await fetch(`${apiBaseUrl}/api/aircraft?bbox=${bbox}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const aircraftList = await res.json();
    aircraftLayer.clearLayers();
    aircraftList.forEach((ac) => {
      L.marker([ac.latitude, ac.longitude], { icon: aircraftIcon(ac.track_deg) })
        .bindPopup(aircraftPopupHtml(ac))
        .addTo(aircraftLayer);
    });
  } catch (err) {
    setStatus(`No se pudieron cargar las aeronaves: ${err.message}`);
  }
}

function toggleAircraftLayer() {
  const enabled = document.getElementById("aircraft-toggle").checked;
  if (enabled) {
    aircraftLayer.addTo(map);
    reloadAircraft();
  } else {
    map.removeLayer(aircraftLayer);
  }
}

// ---------- Wind field (Windy-style arrow grid) ----------
// A grid of wind arrows across the current viewport (not just one per
// active incident - see windLayer/refreshWindVectors above), each carrying
// its own hourly forecast series from /api/fire-spread/wind-field. Dragging
// the bottom timeline scrubber into its future zone re-indexes into that
// already-fetched series (see renderWindFieldAtHour) instead of refetching
// per hour - exactly the pattern the single-incident fire-spread ellipse
// already uses. windFieldFutureHours/Ms feed into the SAME scrubber state
// fire-spread prediction uses (scrubberNowFraction/onScrubberInput below
// take the max of both), so the one control drives whichever of the two
// (or both) is currently active.
let windFieldData = null; // [{lat, lon, hours: [{speed_kmh, direction_from_deg}, ...]}, ...] | null
let windFieldFutureHours = 0;
let windFieldFutureMs = 0;

function clearWindField() {
  windFieldLayer.clearLayers();
}

// hoursAhead uses the same 1-indexed "+Nh" convention as
// applyPredictionEllipse's own fireSpreadData.hourly indexing, so a single
// hoursAhead value from onScrubberInput drives both layers identically.
function renderWindFieldAtHour(hoursAhead) {
  windFieldLayer.clearLayers();
  if (!windFieldData || !windFieldData.length) return;
  windFieldData.forEach((point) => {
    const idx = Math.min(hoursAhead - 1, point.hours.length - 1);
    if (idx < 0) return;
    const hourEntry = point.hours[idx];
    if (!hourEntry) return;
    L.marker([point.lat, point.lon], {
      icon: windArrowIcon(hourEntry.direction_from_deg, hourEntry.speed_kmh),
      interactive: false,
    }).addTo(windFieldLayer);
  });
}

async function reloadWindField() {
  const toggle = document.getElementById("wind-field-toggle");
  if (!toggle || !toggle.checked) return;
  const bounds = map.getBounds();
  const params = new URLSearchParams({
    west: bounds.getWest(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    north: bounds.getNorth(),
    hours: 24,
  });
  try {
    const res = await fetch(`${apiBaseUrl}/api/fire-spread/wind-field?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!toggle.checked) return; // toggled off while this request was in flight
    windFieldData = data;
    windFieldFutureHours = data.length ? Math.max(...data.map((p) => p.hours.length)) : 0;
    windFieldFutureMs = windFieldFutureHours * 3600000;
    updateScrubberEndLabel();
    onScrubberInput(); // re-render at whatever hour the scrubber is currently sitting on
  } catch {
    // Best-effort, same as this app's other Open-Meteo-backed features -
    // a flaky upstream call shouldn't block the rest of the map.
  }
}

function toggleWindFieldLayer() {
  const enabled = document.getElementById("wind-field-toggle").checked;
  if (enabled) {
    reloadWindField();
    return;
  }
  windFieldData = null;
  windFieldFutureHours = 0;
  windFieldFutureMs = 0;
  clearWindField();
  updateScrubberEndLabel();
  // If the scrubber was sitting in a future zone that only existed because
  // of the wind field (no incident prediction of its own keeping it open),
  // snap back to "now" rather than leaving it stranded past the new,
  // possibly-shorter (or zero) future span.
  const range = document.getElementById("scrubber-range");
  if (Number(range.value) > scrubberNowFraction()) range.value = scrubberNowFraction();
  onScrubberInput();
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
    const res = await fetch(`${apiBaseUrl}/api/fire-spread/predict?lat=${lat}&lon=${lon}&max_hours=8`);
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
document.getElementById("aircraft-toggle").addEventListener("change", toggleAircraftLayer);
document.getElementById("wind-field-toggle").addEventListener("change", toggleWindFieldLayer);
document.getElementById("season-burnt-area-toggle").addEventListener("change", toggleSeasonBurntAreaLayer);
document.getElementById("sigpac-toggle").addEventListener("change", toggleSigpacLayer);
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
map.on("moveend", reloadAircraft);
// Same per-viewport pattern for the wind field grid - both moveend (pan)
// and zoomend (grid spacing/point count depends on the bbox span) matter
// here, unlike webcams which only cares about moveend.
map.on("moveend", reloadWindField);
map.on("zoomend", reloadWindField);

// ---------- Filter bar: dropdown pills replacing the old stacked
// label+chip-row groups. Each pill's own label reflects the current
// selection (its specific value when exactly one is picked, a "(n)" count
// otherwise) so the filter state is readable without opening anything -
// the dropdowns themselves stay pure client-side re-derivations of
// lastIncidents, no extra API round-trip on every checkbox click. ----------
const FILTER_RISK_LABELS = { low: "Bajo", moderate: "Moderado", high: "Alto", critical: "Crítico" };
const FILTER_RISK_DOTS = { low: "var(--ok)", moderate: "var(--degraded)", high: "#e8590c", critical: "var(--accent)" };
const FILTER_STATUS_LABELS = { active: "Activo", cooling: "En enfriamiento", archived: "Archivado" };
const FILTER_SOURCE_TOTAL = 4;

function updateFilterPillLabel({ btnId, labelId, dotId, checkedValues: values, labels, totalCount, dots, fallbackLabel }) {
  const btn = document.getElementById(btnId);
  const labelEl = document.getElementById(labelId);
  // Checking none is equivalent to checking all (see incidentPassesFilters) -
  // both mean "no constraint on this facet", so both read as the neutral pill.
  const isFiltered = values.length > 0 && values.length < totalCount;
  btn.classList.toggle("set", isFiltered);
  if (!isFiltered) labelEl.textContent = fallbackLabel;
  else if (values.length === 1) labelEl.textContent = labels[values[0]];
  else labelEl.textContent = `${fallbackLabel} (${values.length})`;
  if (dotId) {
    const dotEl = document.getElementById(dotId);
    dotEl.style.display = isFiltered ? "" : "none";
    dotEl.style.background = values.length === 1 ? dots[values[0]] : "var(--accent)";
  }
}
function updateFilterPillLabels() {
  updateFilterPillLabel({
    btnId: "filter-risk-btn", labelId: "filter-risk-label", dotId: "filter-risk-dot",
    checkedValues: checkedValues(".filter-risk"), labels: FILTER_RISK_LABELS, dots: FILTER_RISK_DOTS,
    totalCount: Object.keys(FILTER_RISK_LABELS).length, fallbackLabel: "Riesgo",
  });
  updateFilterPillLabel({
    btnId: "filter-status-btn", labelId: "filter-status-label",
    checkedValues: checkedValues(".filter-status"), labels: FILTER_STATUS_LABELS,
    totalCount: Object.keys(FILTER_STATUS_LABELS).length, fallbackLabel: "Estado",
  });
  updateFilterPillLabel({
    btnId: "filter-source-btn", labelId: "filter-source-label",
    checkedValues: checkedValues(".filter-source"), labels: {}, totalCount: FILTER_SOURCE_TOTAL, fallbackLabel: "Fuente",
  });
}
document.querySelectorAll(".filter-risk, .filter-status, .filter-source").forEach((checkbox) => {
  checkbox.addEventListener("change", () => {
    updateFilterPillLabels();
    refreshIncidentList();
  });
});
document.getElementById("filter-reset").addEventListener("click", () => {
  // Resets to the app's default view (critical + active), not to "show
  // everything" - that default no longer has its own shortcut button, so
  // this is the only way back to it once you've drifted away.
  document.querySelectorAll(".filter-risk").forEach((el) => (el.checked = el.value === "critical"));
  document.querySelectorAll(".filter-status").forEach((el) => (el.checked = el.value === "active"));
  document.querySelectorAll(".filter-source").forEach((el) => (el.checked = false));
  document.getElementById("locality-search").value = "";
  closeAllFilterDropdowns();
  updateFilterPillLabels();
  refreshIncidentList();
});

// One dropdown open at a time, closed by an outside click, Escape, or
// picking another pill - mirrors the control rail's popover behavior on
// the map side of the app for a consistent interaction language.
function closeAllFilterDropdowns() {
  document.querySelectorAll(".fdropdown.open").forEach((dropdown) => dropdown.classList.remove("open"));
}
document.querySelectorAll(".fbtn[data-dropdown]").forEach((button) => {
  button.addEventListener("click", () => {
    const dropdown = document.getElementById(button.dataset.dropdown);
    const wasOpen = dropdown.classList.contains("open");
    closeAllFilterDropdowns();
    if (!wasOpen) dropdown.classList.add("open");
  });
});
document.addEventListener("click", (e) => {
  if (e.target.closest(".filter-dd")) return;
  closeAllFilterDropdowns();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeAllFilterDropdowns();
});

// ---------- Mobile: floating panels become full-screen overlays, opened one
// at a time via the topbar's sidebar toggle / the control rail's buttons
// instead of being crammed onto a phone-width viewport at once. ----------
function closeAllPopovers() {
  document.querySelectorAll(".rail-popover.open").forEach((popover) => popover.classList.remove("open"));
  document.querySelectorAll(".rail-btn.active").forEach((button) => button.classList.remove("active"));
}
function closeMobilePanels() {
  document.getElementById("incident-sidebar").classList.remove("mobile-open");
  closeAllPopovers();
}
document.getElementById("mobile-sidebar-toggle").addEventListener("click", () => {
  closeAllPopovers();
  document.getElementById("incident-sidebar").classList.add("mobile-open");
});
document.getElementById("mobile-sidebar-close").addEventListener("click", closeMobilePanels);

// ---------- Control rail: each button drops its own popover, one at a time
// (replaces the old always-open right settings sidebar). positionPopover()
// aligns the popover's top edge with whichever button opened it - the rail
// stacks several icons, so a fixed top offset would only line up with the
// first one. On mobile the popover becomes a full-screen sheet instead (see
// the @media rules in index.html), so no positioning is needed there. ----------
function positionPopover(button, popover) {
  if (window.matchMedia("(max-width: 768px)").matches) return;
  popover.style.top = `${button.getBoundingClientRect().top}px`;
}
document.querySelectorAll(".rail-btn[data-popover]").forEach((button) => {
  button.addEventListener("click", () => {
    const popover = document.getElementById(button.dataset.popover);
    const wasOpen = popover.classList.contains("open");
    document.getElementById("incident-sidebar").classList.remove("mobile-open");
    closeAllPopovers();
    if (wasOpen) return;
    positionPopover(button, popover);
    popover.classList.add("open");
    button.classList.add("active");
  });
});
document.querySelectorAll(".popover-close").forEach((button) => {
  button.addEventListener("click", closeAllPopovers);
});
document.addEventListener("click", (e) => {
  if (window.matchMedia("(max-width: 768px)").matches) return;
  if (e.target.closest("#control-rail") || e.target.closest(".rail-popover")) return;
  closeAllPopovers();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeAllPopovers();
});
// Small unread-style dot on the Alertas rail icon while proximity alerts are
// armed, so the one setting that keeps running invisibly in the background
// stays visible at a glance without reopening the popover. Called both from
// the checkbox's own "change" event and everywhere enableLocationAlerts()/
// disableLocationAlerts() set .checked programmatically, since that never
// fires "change" on its own.
function updateAlertsRailDot() {
  document.getElementById("alerts-rail-dot").style.display =
    document.getElementById("location-alerts-toggle").checked ? "" : "none";
}
document.getElementById("location-alerts-toggle").addEventListener("change", updateAlertsRailDot);

document.getElementById("locality-search").addEventListener("input", refreshIncidentList);

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
    const name = displayName(incident);
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
    updateAlertsRailDot();
    return;
  }
  Notification.requestPermission().then((permission) => {
    if (permission !== "granted") {
      setLocationAlertsStatus("Permiso de notificaciones denegado - actívalo en los ajustes del navegador para usar esta función.");
      document.getElementById("location-alerts-toggle").checked = false;
      updateAlertsRailDot();
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
  updateFilterPillLabels();
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
    updateAlertsRailDot();
    enableLocationAlerts();
  }
})();
