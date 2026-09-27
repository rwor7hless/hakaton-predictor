import { RouteLine, Motion } from './motion.js';
import { hav } from './geo.js';
import { fmtDelay, fmtHM, esc, RISK_LABEL, riskOf, STATUS_LABEL, isNum, isLatLon, parseTs } from './format.js';

const STYLES = {
  light: 'https://tiles.openfreemap.org/styles/liberty',
  dark: 'https://tiles.openfreemap.org/styles/fiord',
};
const MOSCOW = [37.618, 55.751];
const MOSCOW_BOUNDS = [
  [36.85, 55.35],
  [38.35, 56.15],
];
const EMPTY = { type: 'FeatureCollection', features: [] };
const RU_LABEL = ['coalesce', ['get', 'name:ru'], ['get', 'name:nonlatin'], ['get', 'name']];
const WIDTH = ['interpolate', ['linear'], ['zoom'], 9, 1.4, 11, 2.2, 13, 3.4, 15, 5, 17, 7];
const plus = (d) => ['interpolate', ['linear'], ['zoom'], 9, 1.4 + d, 11, 2.2 + d, 13, 3.4 + d, 15, 5 + d, 17, 7 + d];
const ZW = [
  [9, 1.4],
  [11, 2.2],
  [13, 3.4],
  [15, 5],
  [17, 7],
];
const zw = (f) => ['interpolate', ['linear'], ['zoom'], ...ZW.flatMap(([z, w]) => [z, f(w)])];
const bySel = (sel, green, other) => zw((w) => ['case', ['get', 'sel'], w + sel, ['==', ['get', 'risk'], 'green'], w + green, w + other]);
const BEHIND_M = 1500;

const P = {};

function readPalette() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
  P.red = v('--map-red', '#ff5b4f');
  P.yellow = v('--map-amber', '#f6a623');
  P.green = v('--map-green', '#2fc49b');
  P.grey = v('--map-grey', '#6d8196');
  P.line = v('--map-line', '#4a7299');
  P.lineSel = v('--map-line-sel', '#9fc0e0');
  P.case = v('--map-case', '#070d13');
  P.ice = v('--map-ice', '#8fc7ff');
  P.stopFill = v('--map-stop-fill', '#0f1923');
  P.stopLine = v('--map-stop-line', '#8aa2ba');
  P.glow = +v('--map-glow', '0.22') || 0.22;
  P.bg = v('--map-bg', '#0b131b');
}

const riskColor = () => ['match', ['get', 'risk'], 'red', P.red, 'yellow', P.yellow, 'green', P.green, P.grey];
const reduceMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const currentTheme = () => (document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');
const toLngLat = (ll) => ll.map((p) => [p[1], p[0]]);

const ROAD = /^(road|tunnel|bridge)_(motorway|trunk|secondary|link|minor|street|service)/;
const DROP = /^(poi_|highway-shield|road_shield|highway_ref|road_one_way|airport|label_country|label_state|place_country|place_continent|place_state|aeroway)/;

function tune(theme, layer) {
  const paint = layer.paint || {};
  if (theme === 'light') {
    if (layer['source-layer'] === 'transportation' && layer.type === 'line' && ROAD.test(layer.id)) {
      if (layer.id.endsWith('_casing')) paint['line-color'] = '#d3d8de';
      else if (/motorway|trunk/.test(layer.id)) paint['line-color'] = '#fdf7ea';
      else paint['line-color'] = '#ffffff';
    }
    if (layer.id === 'building-3d') layer.minzoom = 15.5;
  } else if (theme === 'dark') {
    if (layer.type === 'background') paint['background-color'] = '#2a3447';
    if (layer.id === 'water') paint['fill-color'] = '#1f2a3d';
  }
  layer.paint = paint;
}

const styleCache = new Map();

async function loadStyle(theme) {
  if (styleCache.has(theme)) return styleCache.get(theme);
  const r = await fetch(STYLES[theme]);
  if (!r.ok) throw new Error(`стиль карты: HTTP ${r.status}`);
  const style = await r.json();
  style.layers = style.layers.filter((l) => !DROP.test(l.id));
  for (const layer of style.layers) {
    const tf = layer.layout?.['text-field'];
    if (tf && JSON.stringify(tf).includes('"name')) layer.layout['text-field'] = RU_LABEL;
    tune(theme, layer);
  }
  styleCache.set(theme, style);
  return style;
}

function blankStyle() {
  return { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': P.bg } }] };
}

function chevronImage() {
  const s = 32;
  const c = document.createElement('canvas');
  c.width = s;
  c.height = s;
  const g = c.getContext('2d');
  g.lineCap = 'round';
  g.lineJoin = 'round';
  g.strokeStyle = '#fff';
  g.lineWidth = 6;
  g.beginPath();
  g.moveTo(11, 8);
  g.lineTo(21, 16);
  g.lineTo(11, 24);
  g.stroke();
  return g.getImageData(0, 0, s, s);
}

export function createMap(el, { onSelect, onTiles }) {
  const ml = window.maplibregl;
  readPalette();
  const map = new ml.Map({
    container: el,
    style: blankStyle(),
    center: MOSCOW,
    zoom: 10.3,
    minZoom: 9,
    maxBounds: MOSCOW_BOUNDS,
    renderWorldCopies: false,
    maxZoom: 18.5,
    maxPitch: 60,
    attributionControl: { compact: false, customAttribution: '© участники OpenStreetMap, OpenFreeMap' },
    fadeDuration: 150,
  });
  map.addControl(new ml.NavigationControl({ showCompass: true, visualizePitch: true }), 'top-right');
  const focusControl = document.createElement('div');
  focusControl.className = 'maplibregl-ctrl maplibregl-ctrl-group map-focus-control';
  focusControl.hidden = true;
  const focusButton = document.createElement('button');
  focusButton.type = 'button';
  focusButton.dataset.act = 'focus';
  focusButton.setAttribute('aria-label', 'На карте');
  focusButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" fill-rule="evenodd" d="M12 2a7 7 0 0 0-7 7c0 5 7 13 7 13s7-8 7-13a7 7 0 0 0-7-7Zm0 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/></svg>';
  focusControl.append(focusButton);
  map.addControl({ onAdd: () => focusControl, onRemove: () => focusControl.remove() }, 'top-right');
  map.touchZoomRotate.disableRotation();

  const routes = new Map();
  const markers = new Map();
  const motion = new Motion();
  const targetCache = new Map();
  if (window.__DEBUG_MOTION) {
    window.__motion = motion;
    window.__mapdbg = { map, routes };
  }
  let ready = false;
  let styleTheme = null;
  let selectedRoute = null;
  let showCtx = false;
  let raf = 0;
  let lastVehicles = [];
  let lastOpts = {};
  let lastFocus = {};
  let lastHot = null;
  let focusBounds = null;
  let targetMarker = null;
  let targetMarkerId = null;
  let labelSide = 1;
  let blocks = null;
  let userMoved = false;
  let followAt = 0;
  let tileErrors = 0;
  const wrapEl = el.parentElement;
  // Подписи последних отправленных данных: setData уходит в воркер только при изменениях.
  let aheadSig = null;
  let targetsSig = null;
  let stopsSig = null;

  const popup = new ml.Popup({ closeButton: false, closeOnClick: false, className: 'tip', offset: 14, maxWidth: '320px' });

  function setData(id, data) {
    const s = ready ? map.getSource(id) : null;
    if (s) s.setData(data);
  }

  function addOverlays() {
    const tol = { routes: 0.7, ahead: 0.3, behind: 0.5 };
    for (const id of ['routes', 'stops', 'behind', 'ahead', 'targets', 'hot']) {
      if (!map.getSource(id)) map.addSource(id, { type: 'geojson', data: EMPTY, tolerance: tol[id] ?? 0.375 });
    }
    if (!map.hasImage('chev')) map.addImage('chev', chevronImage(), { pixelRatio: 2, sdf: true });
    const add = (layer) => !map.getLayer(layer.id) && map.addLayer(layer);
    const round = { 'line-cap': 'round', 'line-join': 'round' };
    const exact = ['!', ['get', 'approx']];
    const line = ['==', ['geometry-type'], 'LineString'];
    const hasGlyphs = !!map.getStyle()?.glyphs;
    add({ id: 'routes-case', type: 'line', source: 'routes', filter: exact, layout: round, paint: { 'line-color': P.case, 'line-width': plus(2.4), 'line-opacity': 0.7 } });
    add({ id: 'routes-line', type: 'line', source: 'routes', filter: exact, layout: round, paint: { 'line-color': P.line, 'line-width': plus(-0.4), 'line-opacity': 0.75 } });
    add({ id: 'routes-approx', type: 'line', source: 'routes', filter: ['get', 'approx'], layout: { 'line-cap': 'butt' }, paint: { 'line-color': P.line, 'line-width': plus(-0.8), 'line-opacity': 0.6, 'line-dasharray': [1.2, 1.6] } });
    add({ id: 'routes-dir', type: 'symbol', source: 'routes', filter: ['all', exact, ['get', 'sel']], minzoom: 12, layout: { 'symbol-placement': 'line', 'symbol-spacing': 90, 'icon-image': 'chev', 'icon-size': 0.7, 'icon-allow-overlap': true, 'icon-ignore-placement': true, 'icon-rotation-alignment': 'map' }, paint: { 'icon-color': P.line, 'icon-opacity': 0.85 } });
    add({ id: 'behind-line', type: 'line', source: 'behind', layout: round, paint: { 'line-color': P.grey, 'line-width': plus(0.4), 'line-opacity': 0.8 } });
    add({ id: 'ahead-glow', type: 'line', source: 'ahead', filter: ['any', ['get', 'sel'], ['!=', ['get', 'risk'], 'green']], layout: round, paint: { 'line-color': riskColor(), 'line-width': bySel(14, 10, 10), 'line-blur': 7, 'line-opacity': Math.min(1, P.glow * 2) } });
    add({ id: 'ahead-case', type: 'line', source: 'ahead', layout: round, paint: { 'line-color': P.case, 'line-width': bySel(6, 2.6, 4.2), 'line-opacity': 0.9 } });
    add({ id: 'ahead-line', type: 'line', source: 'ahead', layout: round, paint: { 'line-color': riskColor(), 'line-width': bySel(3.2, 0.6, 2) } });
    add({ id: 'ahead-dir', type: 'symbol', source: 'ahead', filter: ['any', ['get', 'sel'], ['!=', ['get', 'risk'], 'green']], minzoom: 11.5, layout: { 'symbol-placement': 'line', 'symbol-spacing': 60, 'icon-image': 'chev', 'icon-size': ['case', ['get', 'sel'], 0.85, 0.65], 'icon-allow-overlap': true, 'icon-ignore-placement': true, 'icon-rotation-alignment': 'map' }, paint: { 'icon-color': '#ffffff', 'icon-opacity': 0.95 } });
    for (const s of ['hot']) {
      add({ id: `${s}-glow`, type: 'line', source: s, filter: line, layout: round, paint: { 'line-color': ['get', 'color'], 'line-width': plus(18), 'line-blur': 10, 'line-opacity': Math.min(1, P.glow * 2.2) } });
      add({ id: `${s}-case`, type: 'line', source: s, filter: line, layout: round, paint: { 'line-color': P.case, 'line-width': plus(6), 'line-opacity': 0.9 } });
      add({ id: `${s}-line`, type: 'line', source: s, filter: line, layout: round, paint: { 'line-color': ['get', 'color'], 'line-width': plus(2.4) } });
    }
    add({ id: 'stops-all', type: 'circle', source: 'stops', minzoom: 15, paint: { 'circle-radius': 2.8, 'circle-color': P.stopFill, 'circle-stroke-color': P.stopLine, 'circle-stroke-width': 1.6 } });
    add({ id: 'stops-sel', type: 'circle', source: 'stops', filter: ['==', ['get', 'sel'], true], minzoom: 11, paint: { 'circle-radius': ['case', ['get', 'ahead'], 4, 2.8], 'circle-color': P.stopFill, 'circle-stroke-color': ['case', ['get', 'ahead'], P.case, P.stopLine], 'circle-stroke-width': ['case', ['get', 'ahead'], 2.2, 1.4] } });
    add({ id: 'targets-ring', type: 'circle', source: 'targets', filter: ['all', ['!', ['get', 'sel']], ['!=', ['get', 'risk'], 'green']], paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 9, 3.5, 13, 5.5, 16, 7], 'circle-color': P.stopFill, 'circle-stroke-color': riskColor(), 'circle-stroke-width': 2.6 } });
    if (hasGlyphs) {
      add({ id: 'targets-label', type: 'symbol', source: 'targets', filter: ['all', ['!', ['get', 'sel']], ['==', ['get', 'risk'], 'red']], minzoom: 11.5, layout: { 'text-field': ['get', 'short'], 'text-font': ['Noto Sans Bold'], 'text-size': 11.5, 'text-anchor': 'left', 'text-offset': [0.9, 0], 'text-allow-overlap': false }, paint: { 'text-color': P.red, 'text-halo-color': P.stopFill, 'text-halo-width': 1.6 } });
      add({ id: 'stops-sel-label', type: 'symbol', source: 'stops', filter: ['all', ['get', 'sel'], ['get', 'ahead']], minzoom: 13.5, layout: { 'text-field': ['get', 'name'], 'text-font': ['Noto Sans Regular'], 'text-size': 11, 'text-anchor': 'left', 'text-offset': [0.8, 0], 'text-optional': true }, paint: { 'text-color': P.case === '#ffffff' ? '#43515e' : '#c9d5e1', 'text-halo-color': P.stopFill, 'text-halo-width': 1.4 } });
    }
  }

  async function applyStyle(theme) {
    styleTheme = theme;
    try {
      const style = await loadStyle(theme);
      if (styleTheme !== theme) return;
      ready = false;
      map.setStyle(style, { diff: false });
    } catch {
      onTiles?.('down');
      if (!ready) {
        ready = true;
        addOverlays();
        refreshAll();
      }
    }
  }

  map.on('style.load', () => {
    ready = true;
    readPalette();
    addOverlays();
    refreshAll();
    onTiles?.('ok');
  });
  map.on('error', (e) => {
    if (e?.sourceId === 'openmaptiles' || /tile/i.test(String(e?.error?.message || ''))) {
      if (++tileErrors >= 6) onTiles?.('down');
    }
  });
  applyStyle(currentTheme());

  function refreshAll() {
    pushRoutes();
    drawFocus();
    drawHot(false);
    aheadSig = targetsSig = null;
  }

  function splitApprox(r) {
    const ranges = (r.approx || []).filter((x) => x[1] > x[0]).sort((a, b) => a[0] - b[0]);
    if (!ranges.length) return [{ approx: false, coords: r.coords }];
    const parts = [];
    let cur = 0;
    const push = (m0, m1, approx) => {
      if (m1 - m0 < 1) return;
      const c = toLngLat(r.lineIdx.slice(m0, m1));
      if (c.length > 1) parts.push({ approx, coords: c });
    };
    for (const [a, b] of ranges) {
      push(cur, a, false);
      push(a, b, true);
      cur = Math.max(cur, b);
    }
    push(cur, r.lineIdx.length, false);
    return parts;
  }

  function pushRoutes() {
    const feats = [];
    for (const [id, r] of routes) {
      const sel = selectedRoute === id;
      for (const part of splitApprox(r)) {
        feats.push({ type: 'Feature', properties: { id, sel, name: r.name, approx: part.approx }, geometry: { type: 'LineString', coordinates: part.coords } });
      }
    }
    feats.sort((a, b) => Number(a.properties.sel) - Number(b.properties.sel));
    setData('routes', { type: 'FeatureCollection', features: feats });
    stopsSig = aheadSig = null;
    pushStops();
    if (!ready || !map.getLayer('routes-line')) return;
    const has = selectedRoute != null;
    map.setPaintProperty('routes-line', 'line-opacity', has ? ['case', ['get', 'sel'], 0.95, 0.22] : 0.75);
    map.setPaintProperty('routes-case', 'line-opacity', has ? ['case', ['get', 'sel'], 0.8, 0.15] : 0.7);
    map.setPaintProperty('routes-approx', 'line-opacity', has ? ['case', ['get', 'sel'], 0.8, 0.18] : 0.6);
  }

  function pushStops(aheadRange) {
    const cur = routes.get(selectedRoute);
    const ahead = new Set();
    if (cur && aheadRange) {
      const span = cur.lineIdx.delta(aheadRange[0], aheadRange[1]);
      cur.stops.forEach((s, i) => {
        const d0 = isNum(s.m) ? cur.lineIdx.delta(aheadRange[0], +s.m) : NaN;
        if (d0 > 0 && d0 <= span + 1) ahead.add(i);
      });
    }
    const sig = `${selectedRoute}|${[...ahead]}`;
    if (sig === stopsSig) return;
    stopsSig = sig;
    const stops = [];
    for (const [id, r] of routes) {
      const sel = selectedRoute === id;
      r.stops.forEach((s, i) => {
        if (!isLatLon(s.lat, s.lon)) return;
        const q = isNum(s.m) ? r.lineIdx.pointAt(+s.m) : [+s.lat, +s.lon];
        stops.push({ type: 'Feature', properties: { sel, ahead: sel && ahead.has(i), name: s.name || 'Остановка' }, geometry: { type: 'Point', coordinates: [q[1], q[0]] } });
      });
    }
    setData('stops', { type: 'FeatureCollection', features: stops });
  }

  function setRoutes(list) {
    const withGeom = (list || []).filter((r) => Array.isArray(r.path) && r.path.length > 1);
    if (!withGeom.length) return;
    const prev = new Map(routes);
    routes.clear();
    targetCache.clear();
    for (const r of withGeom) {
      const path = r.path.filter((p) => Array.isArray(p) && isLatLon(p[1], p[0]));
      if (path.length < 2) continue;
      const id = String(r.tr_id);
      const coords = path.map((p) => [+p[0], +p[1]]);
      // Та же геометрия — тот же RouteLine: иначе Motion сбросит историю и все ТС разом дёрнутся.
      const old = prev.get(id);
      const same = old && old.closed === !!r.closed && old.coords.length === coords.length && old.coords.every((c, i) => c[0] === coords[i][0] && c[1] === coords[i][1]);
      routes.set(id, {
        id,
        name: r.route_name,
        stops: r.stops || [],
        closed: !!r.closed,
        approx: Array.isArray(r.approx_m) ? r.approx_m : [],
        lineIdx: same ? old.lineIdx : new RouteLine(path, !!r.closed),
        coords,
      });
    }
    pushRoutes();
    pushVehicles();
  }

  function targetM(r, v, m) {
    const p = v.prediction;
    if (!p) return null;
    const lat = isLatLon(p.target_lat, p.target_lon) ? +p.target_lat : +p.segment?.to_lat;
    const lon = isLatLon(p.target_lat, p.target_lon) ? +p.target_lon : +p.segment?.to_lon;
    if (!isLatLon(lat, lon)) return null;
    const key = `${v.tr_id}|${p.target_stop_id}`;
    let cands = targetCache.get(key);
    if (!cands) {
      cands = [];
      r.stops.forEach((s) => {
        if (isNum(s.m) && hav(lat, lon, +s.lat, +s.lon) < 150) cands.push(+s.m);
      });
      const pr = r.lineIdx.project(lat, lon, NaN);
      if (pr.d < 150) cands.push(pr.m);
      targetCache.set(key, cands);
    }
    let best = null;
    for (const tm of cands) {
      const d = r.lineIdx.delta(m, tm);
      const fwd = d >= 0 ? d : r.closed ? d + r.lineIdx.length : -1;
      if (fwd > 5 && fwd < 25000 && (best == null || fwd < best[1])) best = [tm, fwd];
    }
    return best ? best[0] : null;
  }

  function aheadLabel(p) {
    const exp = parseTs(p.target_planned) + (+p.delay_s || 0) * 1000;
    return `${fmtDelay(p.delay_s)} · ${esc(p.target_stop_name || '')}, план ${fmtHM(p.target_planned)}, ожидается ${fmtHM(new Date(exp).toISOString())}`;
  }

  // Каждый кадр: начало «пути до цели» идёт ровно за маркером. Пока ТС стоят, в воркер ничего не уходит.
  function updateAhead(now) {
    if (!ready) return;
    const items = [];
    for (const v of lastVehicles) {
      if (!visible(v) || !v.prediction || !['moving', 'stopped', 'stale'].includes(v.status)) continue;
      const id = String(v.tr_id);
      const r = routes.get(id);
      if (!r) continue;
      const s = motion.sample(id, now);
      if (!s || !Number.isFinite(s.m)) continue;
      const tm = targetM(r, v, s.m);
      // Цвет пути — по статусу ТС (уровню инцидента с гистерезисом), а не по риску отдельного прогноза: так он не мигает.
      if (tm != null) items.push({ v, id, r, m: s.m, tm, risk: riskOf(v.risk), sel: selectedRoute === id, tip: aheadLabel(v.prediction) });
    }
    const tSig = items.map((x) => `${x.id}|${x.risk}|${x.sel}|${x.tm}|${x.tip}`).join(';');
    const sig = `${tSig}|${items.map((x) => x.m.toFixed(1))}`;
    if (sig === aheadSig) return;
    aheadSig = sig;
    const ahead = [];
    const targets = [];
    let behind = EMPTY;
    let selRange = null;
    for (const { v, id, r, m, tm, risk, sel, tip } of items) {
      const coords = toLngLat(r.lineIdx.slice(m, tm));
      if (coords.length < 2) continue;
      ahead.push({ type: 'Feature', properties: { id, risk, sel, tip }, geometry: { type: 'LineString', coordinates: coords } });
      const q = r.lineIdx.pointAt(tm);
      targets.push({ type: 'Feature', properties: { id, risk, sel, short: `${fmtDelay(v.prediction.delay_s)}`, tip }, geometry: { type: 'Point', coordinates: [q[1], q[0]] } });
      if (sel) {
        const b0 = r.closed ? m - BEHIND_M : Math.max(0, m - BEHIND_M);
        if (m - b0 > 5) behind = { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: toLngLat(r.lineIdx.slice(r.lineIdx.wrap(b0), m)) } };
        selRange = [m, tm];
        placeTarget(id, [q[1], q[0]], v.prediction);
      }
    }
    ahead.sort((a, b) => Number(a.properties.sel) - Number(b.properties.sel) || (a.properties.risk === 'green') - (b.properties.risk === 'green'));
    setData('ahead', { type: 'FeatureCollection', features: ahead });
    setData('behind', behind);
    if (tSig !== targetsSig) {
      targetsSig = tSig;
      setData('targets', { type: 'FeatureCollection', features: targets });
    }
    if (selectedRoute) pushStops(selRange);
    if (!selRange && targetMarker && targetMarkerId !== 'focus') removeTarget();
    if (selRange) follow();
  }

  function removeTarget() {
    if (targetMarker) targetMarker.remove();
    targetMarker = null;
    targetMarkerId = null;
  }

  function placeTarget(id, lngLat, p) {
    const html = `<i></i><span><em>Цель прогноза</em><b>${esc(p.target_stop_name || '')}</b>план ${fmtHM(p.target_planned)}, ожидается ${fmtHM(new Date(parseTs(p.target_planned) + (+p.delay_s || 0) * 1000).toISOString())} <strong class="target__d target__d--${riskOf(p.risk)}">${fmtDelay(p.delay_s)}</strong></span>`;
    if (!targetMarker || targetMarkerId !== id) {
      removeTarget();
      const elT = document.createElement('div');
      elT.className = 'target';
      targetMarker = new ml.Marker({ element: elT, anchor: 'center' }).setLngLat(lngLat).addTo(map);
      targetMarkerId = id;
    }
    targetMarker.setLngLat(lngLat);
    const e = targetMarker.getElement();
    if (e._html !== html) {
      e.innerHTML = html;
      e._html = html;
    }
    placeLabel();
  }

  // Элементы поверх карты, под которые не должна уходить подпись цели. Координаты — от угла карты.
  function keepOut() {
    if (blocks) return blocks;
    const box = el.getBoundingClientRect();
    const rect = (q) => {
      const r = wrapEl.querySelector(q)?.getBoundingClientRect();
      return r && r.width && r.height ? { l: r.left - box.left, t: r.top - box.top, r: r.right - box.left, b: r.bottom - box.top } : null;
    };
    blocks = { ctl: rect('.mapctl'), nav: rect('.maplibregl-ctrl-top-right'), legend: rect('.maplegend'), attrib: rect('.maplibregl-ctrl-bottom-right') };
    return blocks;
  }

  // Легенда и атрибуция делят нижний край; если на узкой карте им тесно, легенда встаёт над атрибуцией.
  function fitLegend() {
    blocks = null;
    const lg = wrapEl.querySelector('.maplegend');
    const at = el.querySelector('.maplibregl-ctrl-bottom-right');
    if (!lg || !at) return;
    lg.style.bottom = '';
    const a = at.getBoundingClientRect();
    const l = lg.getBoundingClientRect();
    if (a.width && a.height && l.right + 8 > a.left && l.bottom > a.top) {
      lg.style.bottom = `${Math.ceil(wrapEl.getBoundingClientRect().bottom - a.top) + 6}px`;
    }
  }

  // Подпись ставится по ходу движения (дальше от пути), а если там край карты или кнопки — на другую сторону.
  function placeLabel() {
    const span = targetMarker?.getElement().querySelector('span');
    if (!span) return;
    const W = el.clientWidth;
    const H = el.clientHeight;
    const pt = map.project(targetMarker.getLngLat());
    const onMap = pt.x >= 0 && pt.y >= 0 && pt.x <= W && pt.y <= H;
    span.style.visibility = onMap ? '' : 'hidden';
    if (!onMap) return;
    const bus = markers.get(targetMarkerId)?.marker.getLngLat();
    if (bus) {
      const dx = pt.x - map.project(bus).x;
      if (Math.abs(dx) > 30) labelSide = dx > 0 ? 1 : -1;
    }
    const w = span.offsetWidth;
    const h = span.offsetHeight;
    const R = 9;
    const G = 10;
    const M = 8;
    const right = [pt.x + R + G, pt.y - h / 2];
    const left = [pt.x - R - G - w, pt.y - h / 2];
    const cands = [
      ...(labelSide > 0 ? [right, left] : [left, right]),
      [pt.x - w / 2, pt.y + R + G],
      [pt.x - w / 2, pt.y - R - G - h],
      [pt.x + R + G, pt.y + R],
      [pt.x - R - G - w, pt.y + R],
      [pt.x + R + G, pt.y - R - h],
      [pt.x - R - G - w, pt.y - R - h],
    ];
    const cover = ([x, y]) => {
      let c = (Math.max(0, M - x) + Math.max(0, x + w - W + M)) * h + (Math.max(0, M - y) + Math.max(0, y + h - H + M)) * w;
      for (const b of Object.values(keepOut())) {
        if (b) c += Math.max(0, Math.min(x + w, b.r + 4) - Math.max(x, b.l - 4)) * Math.max(0, Math.min(y + h, b.b + 4) - Math.max(y, b.t - 4));
      }
      return c;
    };
    let best = cands[0];
    let cost = Infinity;
    for (const c of cands) {
      const v = cover(c);
      if (v < cost) [best, cost] = [c, v];
      if (!v) break;
    }
    const x = Math.max(M, Math.min(W - M - w, best[0]));
    const y = Math.max(M, Math.min(H - M - h, best[1]));
    span.style.left = `${Math.round(x - pt.x + R)}px`;
    span.style.top = `${Math.round(y - pt.y + R)}px`;
  }

  // Поля кадра: ниже кнопок, выше легенды, с местом под подпись цели на стороне хода.
  function focusPadding() {
    const W = el.clientWidth;
    const H = el.clientHeight;
    const k = keepOut();
    const pad = { top: (k.ctl?.b || 0) + 30, bottom: H - (k.legend?.t ?? H) + 30, left: 50, right: (k.nav ? W - k.nav.l : 0) + 20 };
    pad[labelSide > 0 ? 'right' : 'left'] += (targetMarker?.getElement().querySelector('span')?.offsetWidth || 240) + 30;
    const sx = Math.min(1, (W * 0.7) / (pad.left + pad.right));
    const sy = Math.min(1, (H * 0.7) / (pad.top + pad.bottom));
    return { top: pad.top * sy, bottom: pad.bottom * sy, left: pad.left * sx, right: pad.right * sx };
  }

  // Цель прогноза сдвигается по маршруту каждые 30 с: камера догоняет её, пока пользователь сам не двигал карту.
  function follow() {
    if (userMoved || !targetMarker || map.isMoving() || performance.now() - followAt < 5000) return;
    const W = el.clientWidth;
    const H = el.clientHeight;
    const k = keepOut();
    const out = [targetMarker.getLngLat(), markers.get(targetMarkerId)?.marker.getLngLat()].some((ll) => {
      if (!ll) return false;
      const p = map.project(ll);
      return p.x < 12 || p.x > W - 12 || p.y < (k.ctl?.b || 12) || p.y > (k.legend?.t ?? H - 12);
    });
    if (!out) return;
    followAt = performance.now();
    drawFocus();
    if (focusBounds) map.fitBounds(focusBounds, { padding: focusPadding(), maxZoom: 15.5, duration: reduceMotion() ? 0 : 900 });
  }

  function markerHtml(v, sel, dim) {
    const risk = riskOf(v.risk);
    const st = v.status;
    const cls = ['bus', `bus--${risk}`];
    if (sel) cls.push('is-sel');
    if (dim) cls.push('is-dim');
    if (st === 'layover') cls.push('is-layover');
    if (st === 'out_of_service' || st === 'offline') cls.push('is-off');
    if (st === 'stale') cls.push('is-stale');
    if (v.scheduled === false) cls.push('is-ctx');
    const d = v.prediction?.delay_s;
    let tag = '';
    if ((risk === 'red' || risk === 'yellow') && isNum(d)) tag = fmtDelay(d);
    else if (st === 'layover' && v.next_trip?.planned) tag = `рейс ${fmtHM(v.next_trip.planned)}`;
    else if (sel) tag = String(v.tr_id);
    return `<div class="${cls.join(' ')}"><i class="bus__halo"></i><i class="bus__dir"></i><i class="bus__core"></i>${tag ? `<span class="bus__tag">${esc(tag)}</span>` : ''}</div>`;
  }

  function tipHtml(v) {
    const risk = riskOf(v.risk);
    const p = v.prediction;
    const lines = [`<b>ТС ${esc(v.tr_id)}</b> <span class="tip__muted">${esc(v.route_name || '')}</span>`];
    if (p) lines.push(`${RISK_LABEL[risk]}: ${fmtDelay(p.delay_s)} у «${esc(p.target_stop_name || '')}» в ${fmtHM(p.target_planned)}`);
    else if (v.status === 'layover' && v.next_trip) lines.push(`На отстое, рейс в ${fmtHM(v.next_trip.planned)}`);
    else lines.push(esc(STATUS_LABEL[v.status] || v.status || ''));
    if (isNum(v.current_delay_s)) lines.push(`<span class="tip__muted">сейчас ${fmtDelay(v.current_delay_s)}</span>`);
    return lines.join('<br>');
  }

  const visible = (v) => v.scheduled !== false || showCtx;
  const tsOf = (v) => {
    const t = parseTs(v.pos_time || v.last_seen);
    return Number.isFinite(t) ? t / 1000 : NaN;
  };

  function routeM(v, r, id) {
    if (isNum(v.route_m)) return +v.route_m;
    if (!r) return NaN;
    const hint = motion.lastM(id);
    const pr = r.lineIdx.project(+v.lat, +v.lon, hint, Number.isFinite(hint) ? 900 : undefined);
    return pr.d < 160 ? pr.m : NaN;
  }

  function pushVehicles() {
    const now = performance.now();
    const { selected, filter, filterIds } = lastOpts;
    const allowed = filterIds ? new Set(filterIds) : null;
    const seen = new Set();
    for (const v of lastVehicles) {
      if (!isLatLon(v.lat, v.lon) || !visible(v)) continue;
      const id = String(v.tr_id);
      seen.add(id);
      const r = routes.get(id);
      const m = routeM(v, r, id);
      motion.push(id, { line: r && Number.isFinite(m) ? r.lineIdx : null, m, lat: +v.lat, lon: +v.lon, heading: isNum(v.heading) ? +v.heading : NaN, ts: tsOf(v) });
      const sel = selected != null && String(selected) === id;
      const dim = ((!!filter && riskOf(v.risk) !== filter) || (allowed && !allowed.has(id)) || (!!selectedRoute && selectedRoute !== id)) && !sel;
      const html = markerHtml(v, sel, dim);
      const z = sel ? 2000 : { red: 900, yellow: 700, green: 400, grey: 100 }[riskOf(v.risk)] + (v.scheduled === false ? -200 : 0);
      let mk = markers.get(id);
      if (!mk) {
        const root = document.createElement('div');
        root.className = 'busicon';
        root.tabIndex = 0;
        root.setAttribute('role', 'button');
        root.setAttribute('aria-label', `ТС ${id}`);
        const start = motion.sample(id, now) || { lat: +v.lat, lon: +v.lon };
        const marker = new ml.Marker({ element: root, anchor: 'center', subpixelPositioning: true }).setLngLat([start.lon, start.lat]).addTo(map);
        const entry = { marker, root, html: '', tip: '' };
        root.addEventListener('click', (e) => {
          e.stopPropagation();
          onSelect?.(entry.tr);
        });
        root.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onSelect?.(entry.tr);
          }
        });
        root.addEventListener('mouseenter', () => {
          popup.setLngLat(marker.getLngLat()).setHTML(entry.tip).addTo(map);
          entry.hover = true;
        });
        root.addEventListener('mouseleave', () => {
          popup.remove();
          entry.hover = false;
        });
        mk = entry;
        markers.set(id, mk);
      }
      mk.tr = v.tr_id;
      mk.tip = tipHtml(v);
      if (mk.html !== html) {
        mk.root.innerHTML = html;
        mk.html = html;
        mk.el = mk.root.firstElementChild;
        mk.hd = null;
        mk.moving = null;
      }
      mk.root.style.zIndex = String(z);
      if (mk.hover) popup.setHTML(mk.tip);
    }
    for (const [id, mk] of markers) {
      if (!seen.has(id)) {
        mk.marker.remove();
        markers.delete(id);
        motion.drop(id);
      }
    }
    kick();
  }

  function frame(now) {
    raf = 0;
    if (document.hidden) return;
    const bearing = map.getBearing();
    for (const [id, mk] of markers) {
      const p = motion.sample(id, now);
      if (!p) continue;
      const cur = mk.last;
      if (!cur || Math.abs(cur[0] - p.lat) > 1e-8 || Math.abs(cur[1] - p.lon) > 1e-8) {
        mk.marker.setLngLat([p.lon, p.lat]);
        mk.last = [p.lat, p.lon];
        if (mk.hover) popup.setLngLat([p.lon, p.lat]);
      }
      if (mk.el && Number.isFinite(p.heading)) {
        const hd = Math.round((p.heading - bearing) * 2) / 2;
        if (mk.hd !== hd) {
          mk.el.style.setProperty('--hd', `${hd}deg`);
          mk.hd = hd;
        }
      }
      if (mk.el && mk.moving !== !!p.moving) {
        mk.el.classList.toggle('is-moving', !!p.moving);
        mk.moving = !!p.moving;
      }
    }
    updateAhead(now);
    if (markers.size) raf = requestAnimationFrame(frame);
  }

  function kick() {
    if (!raf && markers.size && !document.hidden) raf = requestAnimationFrame(frame);
  }
  document.addEventListener('visibilitychange', kick);

  function setVehicles(vehicles, opts = {}) {
    lastVehicles = vehicles || [];
    lastOpts = opts;
    const st = parseTs(opts.streamTime);
    if (Number.isFinite(st)) motion.clock(st / 1000);
    const sel = opts.selected != null ? String(opts.selected) : null;
    const newSel = sel && routes.has(sel) ? sel : null;
    if (newSel !== selectedRoute) {
      selectedRoute = newSel;
      if (!newSel) {
        removeTarget();
        setData('behind', EMPTY);
      }
      pushRoutes();
    }
    pushVehicles();
  }

  function setShowUnscheduled(on) {
    showCtx = !!on;
    pushVehicles();
  }

  function boundsOf(points) {
    const b = new ml.LngLatBounds();
    for (const p of points) b.extend(p);
    return b;
  }

  function drawFocus() {
    const { vehicle, prediction } = lastFocus || {};
    focusBounds = null;
    if (!vehicle) return;
    const id = String(vehicle.tr_id);
    const r = routes.get(id);
    const pts = [];
    if (isLatLon(vehicle.lat, vehicle.lon)) pts.push([+vehicle.lon, +vehicle.lat]);
    if (prediction && r) {
      const s = motion.sample(id);
      const m = s && Number.isFinite(s.m) ? s.m : isNum(vehicle.route_m) ? +vehicle.route_m : NaN;
      if (Number.isFinite(m)) {
        const tm = targetM(r, vehicle, m);
        if (tm != null) {
          pts.push(...toLngLat(r.lineIdx.slice(m, tm)));
          const q = r.lineIdx.pointAt(tm);
          pts.push([q[1], q[0]]);
          if (isLatLon(vehicle.lat, vehicle.lon)) labelSide = q[1] >= +vehicle.lon ? 1 : -1;
          const b0 = r.closed ? m - 600 : Math.max(0, m - 600);
          pts.push(...toLngLat(r.lineIdx.slice(r.lineIdx.wrap(b0), m)));
        }
      }
    }
    if (!prediction && r && isNum(vehicle.route_m)) {
      const m = +vehicle.route_m;
      pts.push(...toLngLat(r.lineIdx.slice(r.lineIdx.wrap(m - 800), r.lineIdx.wrap(m + 1500))));
    }
    if (pts.length) focusBounds = boundsOf(pts);
  }

  function setFocus(args = {}) {
    lastFocus = args;
    const seg = args.prediction?.segment;
    focusControl.hidden = !seg;
    if (seg) {
      const label = `Показать на карте участок ${seg.from_stop || '—'} — ${seg.to_stop || '—'}`;
      focusButton.title = label;
      focusButton.setAttribute('aria-label', label);
    }
    drawFocus();
  }

  function flyToFocus() {
    userMoved = false;
    followAt = performance.now();
    drawFocus();
    if (!focusBounds) return;
    map.fitBounds(focusBounds, { padding: focusPadding(), maxZoom: 15.5, duration: reduceMotion() ? 0 : 900 });
  }

  function segmentByStops(r, seg) {
    if (!seg || !isLatLon(seg.from_lat, seg.from_lon) || !isLatLon(seg.to_lat, seg.to_lon)) return null;
    const straight = [
      [+seg.from_lon, +seg.from_lat],
      [+seg.to_lon, +seg.to_lat],
    ];
    if (!r || !r.stops.length) return straight;
    const near = (lat, lon) =>
      r.stops
        .map((s, i) => [i, isNum(s.m) ? hav(lat, lon, +s.lat, +s.lon) : Infinity])
        .filter((x) => x[1] < 120)
        .sort((a, b) => a[1] - b[1])
        .slice(0, 6)
        .map((x) => x[0]);
    let best = null;
    for (const i of near(+seg.from_lat, +seg.from_lon)) {
      for (const j of near(+seg.to_lat, +seg.to_lon)) {
        const m0 = +r.stops[i].m;
        const m1 = +r.stops[j].m;
        let d = m1 - m0;
        if (d < 0 && r.closed) d += r.lineIdx.length;
        if (d > 0 && d < 20000 && (!best || d < best[2])) best = [m0, m1, d];
      }
    }
    return best ? toLngLat(r.lineIdx.slice(best[0], best[1])) : straight;
  }

  function drawHot(fly) {
    const h = lastHot;
    if (!h) {
      setData('hot', EMPTY);
      return;
    }
    const r = routes.get(String(h.tr_id));
    const coords = segmentByStops(r, h);
    if (!coords) return;
    const share = +h.risk_share || 0;
    const color = share >= 0.6 ? P.red : share >= 0.3 ? P.yellow : P.green;
    setData('hot', { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { color }, geometry: { type: 'LineString', coordinates: coords } }] });
    if (fly) {
      userMoved = true;
      map.fitBounds(boundsOf(coords), { padding: 110, maxZoom: 16, duration: reduceMotion() ? 0 : 900 });
    }
  }

  function setHotspot(h, fly = true) {
    lastHot = h;
    drawHot(fly);
  }

  function fitAll() {
    const all = [];
    for (const r of routes.values()) for (let i = 0; i < r.coords.length; i += 10) all.push(r.coords[i]);
    userMoved = true;
    if (all.length) map.fitBounds(boundsOf(all), { padding: 30, maxZoom: 13, duration: 0 });
  }

  function hoverTip(layer, html) {
    map.on('mousemove', layer, (e) => {
      map.getCanvas().style.cursor = 'pointer';
      const f = e.features?.[0];
      if (f) popup.setLngLat(e.lngLat).setHTML(html(f.properties)).addTo(map);
    });
    map.on('mouseleave', layer, () => {
      map.getCanvas().style.cursor = '';
      popup.remove();
    });
  }
  hoverTip('routes-line', (p) => `<b>${esc(p.name || 'Маршрут')}</b><br><span class="tip__muted">ТС ${esc(p.id)}</span>`);
  hoverTip('routes-approx', (p) => `<b>${esc(p.name || 'Маршрут')}</b><br><span class="tip__muted">Примерный ход: точной геометрии для этого перегона нет</span>`);
  hoverTip('ahead-line', (p) => `<b>Прогноз ТС ${esc(p.id)}</b><br>${p.tip}<br><span class="tip__muted">участок, который ТС пройдёт за 10–15 минут</span>`);
  hoverTip('targets-ring', (p) => `<b>Цель прогноза ТС ${esc(p.id)}</b><br>${p.tip}`);
  hoverTip('stops-sel', (p) => esc(p.name));
  hoverTip('stops-all', (p) => esc(p.name));
  for (const layer of ['routes-line', 'ahead-line', 'targets-ring']) {
    map.on('click', layer, (e) => {
      const id = e.features?.[0]?.properties?.id;
      if (id != null) onSelect?.(id);
    });
  }
  map.on('rotate', () => {
    for (const mk of markers.values()) mk.hd = null;
  });
  map.on('move', placeLabel);
  map.on('movestart', (e) => {
    if (e.originalEvent) userMoved = true;
  });
  map.on('resize', () => {
    fitLegend();
    placeLabel();
  });
  const relayout = new ResizeObserver(() => {
    fitLegend();
    placeLabel();
  });
  for (const node of [el.querySelector('.maplibregl-ctrl-bottom-right'), wrapEl.querySelector('.maplegend'), wrapEl.querySelector('.mapctl')]) {
    if (node) relayout.observe(node);
  }

  function setTheme() {
    readPalette();
    if (currentTheme() !== styleTheme) applyStyle(currentTheme());
  }

  return {
    map,
    setRoutes,
    setVehicles,
    setShowUnscheduled,
    setTheme,
    setFocus,
    setHotspot,
    flyToFocus,
    fitAll,
    hasRoutes: () => routes.size > 0,
    invalidate: () => map.resize(),
  };
}
