const R = 6371000;
const rad = (d) => (d * Math.PI) / 180;

function dist(lat1, lon1, lat2, lon2) {
  const k = Math.cos(rad((lat1 + lat2) / 2));
  return Math.hypot((lon2 - lon1) * k, lat2 - lat1) * rad(1) * R;
}

function bearing(lat1, lon1, lat2, lon2) {
  const k = Math.cos(rad((lat1 + lat2) / 2));
  return ((Math.atan2((lon2 - lon1) * k, lat2 - lat1) * 180) / Math.PI + 360) % 360;
}

export class RouteLine {
  constructor(path, closed = false) {
    const n = path.length;
    this.lat = new Float64Array(n);
    this.lon = new Float64Array(n);
    this.cum = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      this.lon[i] = +path[i][0];
      this.lat[i] = +path[i][1];
      if (i) this.cum[i] = this.cum[i - 1] + dist(this.lat[i - 1], this.lon[i - 1], this.lat[i], this.lon[i]);
    }
    this.length = n ? this.cum[n - 1] : 0;
    this.closed = closed;
  }

  wrap(m) {
    if (!this.closed || !this.length) return Math.max(0, Math.min(this.length, m));
    return ((m % this.length) + this.length) % this.length;
  }

  seek(m) {
    const c = this.cum;
    let lo = 0;
    let hi = c.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (c[mid] <= m) lo = mid;
      else hi = mid;
    }
    return lo;
  }

  pointAt(m) {
    m = this.wrap(m);
    const i = this.seek(m);
    const j = Math.min(i + 1, this.cum.length - 1);
    const L = this.cum[j] - this.cum[i];
    const u = L > 0 ? (m - this.cum[i]) / L : 0;
    return [this.lat[i] + (this.lat[j] - this.lat[i]) * u, this.lon[i] + (this.lon[j] - this.lon[i]) * u];
  }

  bearingAt(m, span = 12) {
    const a = this.pointAt(m - span);
    const b = this.pointAt(m + span);
    return bearing(a[0], a[1], b[0], b[1]);
  }

  slice(m0, m1) {
    const out = [this.pointAt(m0)];
    if (m1 < m0 && this.closed) {
      for (let i = this.seek(m0) + 1; i < this.cum.length; i++) out.push([this.lat[i], this.lon[i]]);
      for (let i = 0; i <= this.seek(m1); i++) out.push([this.lat[i], this.lon[i]]);
    } else {
      for (let i = this.seek(m0) + 1; i <= this.seek(m1); i++) out.push([this.lat[i], this.lon[i]]);
    }
    out.push(this.pointAt(m1));
    return out;
  }

  project(lat, lon, hint, win = 800) {
    let best = Infinity;
    let bm = NaN;
    const n = this.cum.length;
    const k = Math.cos(rad(lat));
    const scan = (lo, hi) => {
      for (let i = Math.max(0, this.seek(lo)); i < n - 1 && this.cum[i] <= hi; i++) {
        const ax = (this.lon[i + 1] - this.lon[i]) * k;
        const ay = this.lat[i + 1] - this.lat[i];
        const px = (lon - this.lon[i]) * k;
        const py = lat - this.lat[i];
        const L2 = ax * ax + ay * ay;
        const u = L2 > 0 ? Math.max(0, Math.min(1, (px * ax + py * ay) / L2)) : 0;
        const d = Math.hypot(px - u * ax, py - u * ay) * rad(1) * R;
        if (d < best) {
          best = d;
          bm = this.cum[i] + u * (this.cum[i + 1] - this.cum[i]);
        }
      }
    };
    if (Number.isFinite(hint)) {
      scan(hint - win, hint + win);
      if (this.closed && hint + win > this.length) scan(0, hint + win - this.length);
      if (this.closed && hint - win < 0) scan(this.length + hint - win, this.length);
    } else {
      scan(0, this.length);
    }
    return { m: bm, d: best };
  }

  delta(m0, m1) {
    let d = m1 - m0;
    if (this.closed && this.length) {
      d = ((d % this.length) + this.length) % this.length;
      if (d > this.length / 2) d -= this.length;
    }
    return d;
  }
}

const JITTER_BACK_M = 80;
const TELEPORT_M = 3500;
const MAX_SAMPLES = 10;
const CATCHUP_S = 2;
const SMOOTH_S = 0.35;

// SmoothDamp (Game Programming Gems 4): критически демпфированная погоня за целью.
// Скорость меняется плавно, при остановке цели нет перелёта, устойчиво при любом dt.
// e — насколько цель впереди, v — текущая скорость; возвращает [шаг, новая скорость].
function damp(v, e, dt) {
  const w = 2 / SMOOTH_S;
  const x = w * dt;
  const k = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x);
  const tmp = (v - w * e) * dt;
  return [e + (tmp - e) * k, (v - w * tmp) * k];
}

export class Motion {
  constructor() {
    this.tracks = new Map();
    this.anchor = null;
    this.rate = 1;
    this.gap = 12;
    this.lats = [];
    this.lagTarget = 30;
  }

  get lag() {
    return this.lagTarget;
  }

  noteLatency(lat) {
    this.lats.push(lat);
    if (this.lats.length > 240) this.lats.shift();
    const sorted = [...this.lats].sort((a, b) => a - b);
    const med = sorted[sorted.length >> 1];
    const ok = sorted.filter((x) => x <= med * 3 + 10);
    const q = ok[Math.min(ok.length - 1, Math.floor(ok.length * 0.99))];
    this.lagTarget = Math.max(3, Math.min(150, q + this.rate * 0.5 + 1));
  }

  clock(streamSec, now = performance.now()) {
    if (!Number.isFinite(streamSec)) return;
    const last = this.anchor;
    if (!last || streamSec < last.lastStream - 120) {
      if (last) this.tracks.clear();
      this.anchor = { c0: streamSec, w0: now, corr: 0, lastStream: streamSec, lastWall: now };
      return;
    }
    const c = this.raw(now);
    const dw = (now - last.lastWall) / 1000;
    if (dw > 0.2) {
      const r = Math.max(0, Math.min(600, (streamSec - last.lastStream) / dw));
      this.rate = this.rate * 0.7 + r * 0.3;
    }
    const e = streamSec - c;
    if (Math.abs(e) > Math.max(60, this.rate * 5)) {
      this.anchor = { c0: streamSec, w0: now, corr: 0, lastStream: streamSec, lastWall: now };
      return;
    }
    const corr = Math.max(-this.rate * 0.8, e / CATCHUP_S);
    this.anchor = { c0: c, w0: now, corr, lastStream: streamSec, lastWall: now };
  }

  raw(wall) {
    const a = this.anchor;
    if (!a) return NaN;
    const dt = Math.max(0, (wall - a.w0) / 1000);
    return a.c0 + dt * this.rate + a.corr * Math.min(dt, CATCHUP_S);
  }

  now(wall = performance.now()) {
    const target = this.lag;
    if (this.lagCur == null) {
      this.lagCur = target;
      this.lagWall = wall;
    }
    const dt = Math.max(0, (wall - this.lagWall) / 1000);
    this.lagWall = wall;
    // Запас набираем быстро (лучше притормозить, чем встать), отдаём медленно — без ускорений.
    const step = (target > this.lagCur ? 0.3 : 0.05) * Math.max(this.rate, 1) * dt;
    this.lagCur += Math.max(-step, Math.min(step, target - this.lagCur));
    return this.raw(wall) - this.lagCur;
  }

  push(id, { line, m, lat, lon, heading, ts }) {
    let tr = this.tracks.get(id);
    if (!tr) {
      tr = { samples: [], heading: Number.isFinite(heading) ? heading : NaN, line: null, v: 0, vx: 0, vy: 0 };
      this.tracks.set(id, tr);
    }
    const onLine = !!line && Number.isFinite(m);
    if (onLine !== !!tr.line || (onLine && tr.line !== line)) tr.samples = [];
    tr.line = onLine ? line : null;
    if (!onLine && Number.isFinite(heading)) tr.rawHeading = heading;
    const last = tr.samples[tr.samples.length - 1];
    if (last && Number.isFinite(ts) && ts <= last.ts) return;
    // Сколько отставать, чтобы не обогнать данные: возраст прежней свежей точки в момент,
    // когда пришла следующая (доставка + интервал телеметрии). Иначе маркер замирает и догоняет рывком.
    const clk = this.raw(performance.now());
    if (last && Number.isFinite(ts) && Number.isFinite(clk)) {
      const age = clk - last.ts;
      if (age >= 0 && age < 600) this.noteLatency(age);
    }
    if (last && Number.isFinite(ts) && ts - last.ts < 120) this.gap = this.gap * 0.9 + (ts - last.ts) * 0.1;
    const s = { ts: Number.isFinite(ts) ? ts : (last ? last.ts + this.gap : 0), m: onLine ? m : NaN, lat, lon };
    if (last && onLine) {
      const d = line.delta(last.m, m);
      if (d < 0 && d > -JITTER_BACK_M) s.m = last.m;
      if (Math.abs(d) > TELEPORT_M) tr.samples = [];
    } else if (last && !onLine && dist(last.lat, last.lon, lat, lon) > TELEPORT_M) {
      tr.samples = [];
    }
    tr.samples.push(s);
    if (tr.samples.length > MAX_SAMPLES) tr.samples.shift();
  }

  drop(id) {
    this.tracks.delete(id);
  }

  lastM(id) {
    const tr = this.tracks.get(id);
    if (!tr || !tr.line) return NaN;
    if (Number.isFinite(tr.dispM)) return tr.dispM;
    const last = tr.samples[tr.samples.length - 1];
    return last ? last.m : NaN;
  }

  has(id) {
    return this.tracks.has(id);
  }

  sample(id, wall = performance.now()) {
    const tr = this.tracks.get(id);
    if (!tr || !tr.samples.length) return null;
    const ss = tr.samples;
    let t = this.now(wall);
    if (!Number.isFinite(t)) t = ss[ss.length - 1].ts;
    let k = ss.length - 1;
    while (k > 0 && ss[k - 1].ts > t) k--;
    const b = ss[k];
    const a = k > 0 ? ss[k - 1] : null;
    const u = a && b.ts > a.ts ? Math.max(0, Math.min(1, (t - a.ts) / (b.ts - a.ts))) : t >= b.ts || !a ? 1 : 0;
    const dtw = tr.wall ? Math.max(0, Math.min(0.2, (wall - tr.wall) / 1000)) : 0;
    tr.wall = wall;
    // Между точками телеметрии цель движется линейно, маркер догоняет её через damp:
    // без него скорость скачком меняется на каждой новой точке.
    if (tr.line) {
      const line = tr.line;
      const d = a ? line.delta(a.m, b.m) : 0;
      let m = a ? line.wrap(a.m + d * u) : b.m;
      const e = Number.isFinite(tr.dispM) && tr.dispLine === line ? line.delta(tr.dispM, m) : Infinity;
      if (Math.abs(e) < TELEPORT_M) {
        const [step, v] = damp(tr.v, e, dtw);
        m = line.wrap(tr.dispM + step);
        tr.v = v;
      } else tr.v = 0;
      tr.dispM = m;
      tr.dispLine = line;
      tr.disp = null;
      const moving = !!a && Math.abs(d) > 1 && u < 1;
      if (moving || !Number.isFinite(tr.heading)) tr.heading = line.bearingAt(m);
      const p = line.pointAt(m);
      return { lat: p[0], lon: p[1], heading: tr.heading, moving, m };
    }
    tr.dispLine = null;
    let lat = a ? a.lat + (b.lat - a.lat) * u : b.lat;
    let lon = a ? a.lon + (b.lon - a.lon) * u : b.lon;
    if (tr.disp && dist(tr.disp[0], tr.disp[1], lat, lon) < TELEPORT_M) {
      const [sy, vy] = damp(tr.vy, lat - tr.disp[0], dtw);
      const [sx, vx] = damp(tr.vx, lon - tr.disp[1], dtw);
      lat = tr.disp[0] + sy;
      lon = tr.disp[1] + sx;
      tr.vy = vy;
      tr.vx = vx;
    } else tr.vy = tr.vx = 0;
    tr.disp = [lat, lon];
    const moving = !!a && u < 1 && dist(a.lat, a.lon, b.lat, b.lon) > 3;
    if (moving) tr.heading = bearing(a.lat, a.lon, b.lat, b.lon);
    else if (Number.isFinite(tr.rawHeading)) tr.heading = tr.rawHeading;
    return { lat, lon, heading: tr.heading, moving };
  }
}
