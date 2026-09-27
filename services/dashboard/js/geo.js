import { isLatLon } from './format.js';

const R = 6371000;
const rad = (d) => (d * Math.PI) / 180;

export function hav(lat1, lon1, lat2, lon2) {
  const dp = rad(lat2 - lat1);
  const dl = rad(lon2 - lon1);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

export function bearing(lat1, lon1, lat2, lon2) {
  const y = Math.sin(rad(lon2 - lon1)) * Math.cos(rad(lat2));
  const x = Math.cos(rad(lat1)) * Math.sin(rad(lat2)) - Math.sin(rad(lat1)) * Math.cos(rad(lat2)) * Math.cos(rad(lon2 - lon1));
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

function fastDist(lat1, lon1, lat2, lon2) {
  const k = Math.cos(rad((lat1 + lat2) / 2));
  const dx = (lon2 - lon1) * 111320 * k;
  const dy = (lat2 - lat1) * 110540;
  return Math.hypot(dx, dy);
}

export function validPath(path) {
  return Array.isArray(path) ? path.filter((p) => Array.isArray(p) && isLatLon(p[1], p[0])).map((p) => [+p[0], +p[1]]) : [];
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  return s[s.length >> 1];
}

export function despike(path, win = 7, maxM = 800) {
  if (path.length < win) return path;
  const gaps = [];
  for (let i = 1; i < path.length; i++) gaps.push(fastDist(path[i - 1][1], path[i - 1][0], path[i][1], path[i][0]));
  if (median(gaps) > 250) return path;
  const h = win >> 1;
  const far = path.map((p, i) => {
    const lo = Math.max(0, Math.min(i - h, path.length - win));
    const w = path.slice(lo, lo + win);
    return fastDist(p[1], p[0], median(w.map((q) => q[1])), median(w.map((q) => q[0]))) > maxM;
  });
  const out = [];
  for (let i = 0; i < path.length; ) {
    if (!far[i]) {
      out.push(path[i++]);
      continue;
    }
    let j = i;
    while (j < path.length && far[j]) j++;
    const before = path[i - 1];
    const after = path[j];
    const spike = j - i <= 3 && before && after && fastDist(before[1], before[0], after[1], after[0]) < maxM;
    if (!spike) for (let k = i; k < j; k++) out.push(path[k]);
    i = j;
  }
  return out;
}

export function alignStops(stops, path) {
  const n = path.length;
  const m = stops.length;
  if (!m) return [];
  if (n < 2) return stops.map(() => 0);
  let prev = new Float64Array(n);
  const back = [];
  for (let p = 0; p < n; p++) prev[p] = fastDist(stops[0].lat, stops[0].lon, path[p][1], path[p][0]);
  back.push(null);
  for (let s = 1; s < m; s++) {
    const cur = new Float64Array(n);
    const br = new Int32Array(n);
    let best = Infinity;
    let bi = 0;
    for (let p = 0; p < n; p++) {
      if (prev[p] < best) {
        best = prev[p];
        bi = p;
      }
      cur[p] = best + fastDist(stops[s].lat, stops[s].lon, path[p][1], path[p][0]);
      br[p] = bi;
    }
    back.push(br);
    prev = cur;
  }
  let pi = 0;
  for (let p = 1; p < n; p++) if (prev[p] < prev[pi]) pi = p;
  const idx = new Array(m);
  idx[m - 1] = pi;
  for (let s = m - 1; s > 0; s--) {
    pi = back[s][pi];
    idx[s - 1] = pi;
  }
  return idx;
}

export function cumDist(path) {
  const out = new Float64Array(path.length);
  for (let i = 1; i < path.length; i++) {
    out[i] = out[i - 1] + hav(path[i - 1][1], path[i - 1][0], path[i][1], path[i][0]);
  }
  return out;
}

export function pointAt(path, cum, d) {
  const n = path.length;
  if (!n) return null;
  if (n === 1 || d <= 0) return { lat: path[0][1], lon: path[0][0], i: 0 };
  if (d >= cum[n - 1]) return { lat: path[n - 1][1], lon: path[n - 1][0], i: n - 2 };
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= d) lo = mid;
    else hi = mid;
  }
  const seg = cum[hi] - cum[lo] || 1;
  const f = (d - cum[lo]) / seg;
  return {
    lat: path[lo][1] + (path[hi][1] - path[lo][1]) * f,
    lon: path[lo][0] + (path[hi][0] - path[lo][0]) * f,
    i: lo,
  };
}

export function sliceLatLngs(path, i0, i1) {
  const a = Math.max(0, Math.min(i0, i1));
  const b = Math.min(path.length - 1, Math.max(i0, i1));
  return path.slice(a, b + 1).map((p) => [p[1], p[0]]);
}
