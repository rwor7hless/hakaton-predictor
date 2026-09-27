import { alignStops, cumDist, pointAt, bearing, hav, validPath } from './geo.js';
import { toIso, hmsToSec, fmtDelay, fmtNum } from './format.js';

const DAY0 = Date.UTC(2026, 0, 6);
const STEP_MS = 30000;
const LAYOVER_S = 300;
const DWELL_S = 20;
const HISTORY_MIN = 60;
const LATE_S = 120;
const CAUSE_SHORT = {
  slow_segment: 'низкая скорость на участке',
  dwell_off_stop: 'простой вне остановки',
  traffic_jam: 'затор по ходу движения',
  bunching: 'сближение с впереди идущим ТС',
  accumulated_delay: 'накопленное опоздание',
  delay_trend: 'растущее отставание',
  peak_hour: 'час пик',
  early_running: 'опережение графика',
  free_flow: 'свободная дорога',
  low_demand: 'мало пассажиров',
  on_schedule: 'движение по графику',
};
const EARLY_S = -60;
// «Контекстные» машины из телеметрии: расписания нет, прогноз не строится (как на стенде).
const UNSCHEDULED = [
  { tr_id: 900101, unit_id: 890371, near: 0 },
  { tr_id: 900102, unit_id: 815631, near: 3 },
  { tr_id: 900103, unit_id: 1112060, near: null },
  { tr_id: 900104, unit_id: 794446, near: null },
];
const idle = (p) => p.offDuty || p.layover;

const PROFILES = [
  { base: 35, amp: 40, per: 53, wave: 380, waveP: 72, wavePh: 0.18, cause: 'slow_segment' },
  { base: 20, amp: 30, per: 41 },
  { base: -125, amp: 35, per: 37, early: true },
  { base: 55, amp: 45, per: 61, wave: 320, waveP: 58, wavePh: 0.05, cause: 'dwell_off_stop' },
  { base: 10, amp: 25, per: 45 },
  { base: 50, amp: 55, per: 49 },
  { base: 70, amp: 40, per: 66, wave: 420, waveP: 88, wavePh: 0.62, cause: 'traffic_jam' },
  { base: 5, amp: 20, per: 33, layover: true },
  { base: 30, amp: 35, per: 39, wave: 260, waveP: 50, wavePh: 0.82, cause: 'bunching' },
  { base: 85, amp: 50, per: 57 },
  { base: 15, amp: 30, per: 47 },
  { base: 40, amp: 20, per: 50, stale: true },
  { base: 0, amp: 0, per: 60, offDuty: true },
];

function hash01(a, b) {
  let h = (Math.imul(a | 0, 374761393) + Math.imul(b | 0, 668265263)) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

function gauss(a, b) {
  const u = Math.max(1e-9, hash01(a, b));
  const v = hash01(b + 7919, a + 104729);
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function erf(x) {
  const s = Math.sign(x);
  const t = 1 / (1 + 0.3275911 * Math.abs(x));
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return s * y;
}

const Phi = (z) => 0.5 * (1 + erf(z / Math.SQRT2));
const round = (x, d = 0) => Math.round(x * 10 ** d) / 10 ** d;
const later = (v, ms = 60) => new Promise((r) => setTimeout(() => r(v == null ? v : structuredClone(v)), ms + Math.random() * 60));

function riskFrom(pLate, pEarly) {
  if (pLate >= 0.6 || pEarly >= 0.8) return 'red';
  if (pLate >= 0.35 || pEarly >= 0.5) return 'yellow';
  return 'green';
}

function predNoise(v, T, scale) {
  const s = T / 1000;
  return scale * (0.55 * Math.sin((2 * Math.PI * s) / 1380 + v.idx * 2.1) + 0.35 * Math.sin((2 * Math.PI * s) / 540 + v.idx * 0.7) + 0.35 * gauss(v.tr_id, Math.floor(T / STEP_MS)));
}

function prepare(r, idx) {
  const stops = r.stops.map((s) => ({ ...s, lat: +s.lat, lon: +s.lon, t: +s.t }));
  let path = validPath(r.path);
  if (path.length < 2) path = stops.map((s) => [s.lon, s.lat]);
  const cum = cumDist(path);
  const al = alignStops(stops, path);
  const stopD = al.map((k) => cum[k]);
  for (let i = 1; i < stopD.length; i++) stopD[i] = Math.max(stopD[i], stopD[i - 1]);
  const loopS = Math.max(+r.loop_s || stops[stops.length - 1].t, 60);
  return {
    idx,
    tr_id: r.tr_id,
    unit_id: r.unit_id,
    route_name: r.route_name,
    stops,
    path,
    cum,
    stopD,
    loopS,
    periodMs: (loopS + LAYOVER_S) * 1000,
    baseMs: DAY0 + hmsToSec(r.loop_start || '07:00:00') * 1000,
    prof: PROFILES[idx % PROFILES.length],
    predLog: [],
    lastPred: null,
    lastP: null,
  };
}

function waveAt(v, t) {
  const p = v.prof;
  if (!p.wave) return 0;
  const s = (t - DAY0) / 1000;
  return p.wave * Math.max(0, Math.sin(2 * Math.PI * (s / (p.waveP * 60) + p.wavePh))) ** 3;
}

function delayAt(v, t) {
  const p = v.prof;
  const s = (t - DAY0) / 1000;
  return p.base + p.amp * Math.sin((2 * Math.PI * s) / (p.per * 60) + v.idx) + 14 * Math.sin((2 * Math.PI * s) / 437 + v.idx * 1.7) + waveAt(v, t);
}

const cycleOf = (v, sched) => Math.floor((sched - v.baseMs) / v.periodMs);
const cycleStart = (v, n) => v.baseMs + n * v.periodMs;
const stopId = (v, n, i) => String(Number(v.stops[i].stop_id) + n * 100000);

function planPos(v, sched) {
  const n = cycleOf(v, sched);
  const off = (sched - cycleStart(v, n)) / 1000;
  const st = v.stops;
  const last = st.length - 1;
  if (off >= st[last].t) return { n, i: last, d: v.stopD[last], dwell: true, layover: true };
  let i = 0;
  while (i < last - 1 && st[i + 1].t <= off) i++;
  const t0 = st[i].t;
  const seg = st[i + 1].t - t0;
  if (seg <= 0) return { n, i, d: v.stopD[i + 1], dwell: false };
  const dw = Math.min(DWELL_S, seg * 0.3);
  if (off - t0 < dw) return { n, i, d: v.stopD[i], dwell: true };
  const f = (off - t0 - dw) / (seg - dw);
  const e = 0.5 - 0.5 * Math.cos(Math.PI * Math.min(1, Math.max(0, f)));
  return { n, i, d: v.stopD[i] + (v.stopD[i + 1] - v.stopD[i]) * e, dwell: false };
}

function positionAt(v, t) {
  const d = delayAt(v, t);
  const pp = planPos(v, t - d * 1000);
  const pt = pointAt(v.path, v.cum, pp.d);
  return { ...pp, lat: pt.lat, lon: pt.lon, delay: d };
}

function* arrivals(v, fromMs, cycles = 4) {
  let n = cycleOf(v, fromMs);
  for (let k = 0; k < cycles; k++, n++) {
    const c = cycleStart(v, n);
    for (let i = 0; i < v.stops.length; i++) yield { n, i, planned: c + v.stops[i].t * 1000 };
  }
}

function predictAt(v, T, fallbackModel) {
  const lo = T + 10 * 60000;
  const hi = T + 15 * 60000;
  let tgt = null;
  let first = null;
  for (const a of arrivals(v, T - v.periodMs, 5)) {
    if (a.planned <= lo) continue;
    if (!first) first = a;
    if (a.planned <= hi) {
      tgt = a;
      break;
    }
    break;
  }
  tgt = tgt || first;
  if (!tgt) return null;
  const leadMin = (tgt.planned - T) / 60000;
  const truth = delayAt(v, tgt.planned);
  const pred = truth + predNoise(v, T, fallbackModel ? 150 : 90);
  const sigma = (fallbackModel ? 90 : 55) + 2.5 * leadMin;
  const pLate = 1 - Phi((LATE_S - pred) / sigma);
  const pEarly = Phi((EARLY_S - pred) / sigma);
  return {
    T,
    tgt,
    leadMin,
    truth,
    pred,
    q10: pred - 1.2816 * sigma,
    q90: pred + 1.2816 * sigma,
    pLate,
    pEarly,
    risk: riskFrom(pLate, pEarly),
  };
}

function isPeak(t) {
  const h = new Date(t).getUTCHours();
  return (h >= 7 && h < 10) || (h >= 17 && h < 20);
}

function segStops(v, a, b) {
  const s = v.stops;
  return {
    from_stop: s[a].name,
    to_stop: s[b].name,
    from_lat: s[a].lat,
    from_lon: s[a].lon,
    to_lat: s[b].lat,
    to_lon: s[b].lon,
  };
}

function buildPrediction(v, P, fallbackModel) {
  const cur = positionAt(v, P.T);
  const last = v.stops.length - 1;
  const wave = waveAt(v, P.tgt.planned);
  const causes = [];
  let segFrom;
  let segTo;
  const waveCause = P.pred >= 60 && wave > 60;
  if (waveCause) {
    segFrom = Math.min(cur.i, last - 1);
    segTo = segFrom + 1;
  } else {
    segTo = Math.max(1, P.tgt.i);
    segFrom = segTo - 1;
  }
  if (v.stops[segFrom].name === v.stops[segTo].name) {
    if (segTo < last) segTo++;
    else if (segFrom > 0) segFrom--;
  }
  const seg = segStops(v, segFrom, segTo);
  const lastName = v.stops[cur.i].name;
  const slope = (delayAt(v, P.T) - delayAt(v, P.T - 600000)) / 10;
  if (P.pred >= 60) {
    if (waveCause) {
      const texts = {
        slow_segment: `Низкая скорость на участке «${seg.from_stop} → ${seg.to_stop}»: ${Math.max(4, Math.round(21 - wave / 25))} км/ч при норме 21`,
        dwell_off_stop: `Простой ${Math.max(2, Math.round(wave / 90))} мин вне остановки`,
        traffic_jam: `Затор по ходу движения: средняя скорость ${Math.max(5, Math.round(18 - wave / 40))} км/ч за 10 мин`,
        bunching: 'Сближение с впереди идущим ТС: интервал 2 мин вместо 8',
      };
      causes.push({ code: v.prof.cause, text: texts[v.prof.cause], weight: 0.35 + wave / 1000 });
    }
    if (cur.delay > 45) causes.push({ code: 'accumulated_delay', text: `Накопленное опоздание ${fmtDelay(cur.delay)} у остановки «${lastName}»`, weight: 0.2 + Math.min(0.3, cur.delay / 900) });
    if (slope > 3) causes.push({ code: 'delay_trend', text: `Отставание растёт: ${fmtDelay(slope * 10)} за последние 10 мин`, weight: 0.12 + Math.min(0.2, slope / 60) });
    if (isPeak(P.T)) causes.push({ code: 'peak_hour', text: 'Час пик: по истории участок проходят на 1–2 мин дольше графика', weight: 0.12 });
    if (!causes.length) causes.push({ code: 'accumulated_delay', text: 'Систематическое отставание от графика на маршруте', weight: 0.5 });
  } else if (P.pred <= -30) {
    causes.push({ code: 'early_running', text: `Опережение графика ${fmtDelay(cur.delay)} у остановки «${lastName}»`, weight: 0.6 });
    causes.push({ code: 'free_flow', text: 'Свободная дорога: скорость на 30 % выше нормы', weight: 0.3 });
    if (!isPeak(P.T)) causes.push({ code: 'low_demand', text: 'Мало пассажиров: стоянки короче плановых', weight: 0.1 });
  } else {
    causes.push({ code: 'on_schedule', text: 'Движение близко к графику', weight: 1 });
  }
  const wsum = causes.reduce((s, c) => s + c.weight, 0) || 1;
  causes.forEach((c) => (c.weight = round(c.weight / wsum, 2)));
  causes.sort((a, b) => b.weight - a.weight);

  const tgtName = v.stops[P.tgt.i].name;
  let recommendations = [];
  if (P.risk !== 'green' && P.pred >= 0) {
    recommendations =
      P.risk === 'red'
        ? [
            `Выпустить резервное ТС с конечной «${v.stops[0].name}»`,
            `Сократить стоянку на конечной до 2 мин`,
            `Сообщить водителю ТС ${v.tr_id}: проезд «${seg.from_stop} → ${seg.to_stop}» без задержек`,
          ]
        : [`Проконтролировать проезд участка «${seg.from_stop} → ${seg.to_stop}»`, `Предупредить водителя о риске опоздания к «${tgtName}»`];
  } else if (P.risk !== 'green') {
    recommendations = [
      `Удержать ТС на остановке «${v.stops[Math.min(cur.i + 1, last)].name}» на ${Math.max(1, Math.round(-P.pred / 60))} мин`,
      'Напомнить водителю о запрете опережения графика',
    ];
  }
  return {
    issued_at: toIso(P.T),
    target_stop_id: stopId(v, P.tgt.n, P.tgt.i),
    target_stop_name: tgtName,
    target_planned: toIso(P.tgt.planned),
    lead_min: round(P.leadMin, 1),
    delay_s: round(P.pred),
    delay_q10_s: round(P.q10),
    delay_q90_s: round(P.q90),
    p_late: round(P.pLate, 2),
    p_early: round(P.pEarly, 2),
    risk: P.risk,
    causes,
    segment: seg,
    recommendations,
    model: fallbackModel ? 'fallback' : 'ensemble',
    _seg: [segFrom, segTo],
    _cur: [cur.n, cur.i],
    _tgt: [P.tgt.n, P.tgt.i],
  };
}

function publicPrediction(p) {
  if (!p) return null;
  const { _seg, _cur, _tgt, ...rest } = p;
  return rest;
}

export async function createMockApi({ reason = 'forced', routesUrl = 'mock/routes.json' } = {}) {
  const res = await fetch(routesUrl, { cache: 'no-store' });
  if (!res.ok) throw new Error('Не удалось загрузить mock/routes.json');
  const raw = await res.json();
  const vehicles = raw.map(prepare);
  const qs = new URLSearchParams(location.search);
  const forceDegraded = qs.get('degraded') === '1';
  const bootedAt = performance.now();

  const clock = {
    t: DAY0 + hmsToSec(qs.get('start') || '07:40') * 1000,
    speed: Number(qs.get('speed')) || 20,
    state: 'running',
    startTime: (qs.get('start') || '07:40').slice(0, 5),
    wallAt: performance.now(),
    pausedWall: null,
    stoppedWall: null,
    sent: 0,
    lastStep: 0,
  };
  const alerts = new Map();
  // Отметка «в работе» относится к инциденту по ТС, а не к отдельной цели прогноза.
  const ackByTr = new Map();
  let resolved = [];
  let predictionsTotal = 0;
  let packetsTotal = 48213;

  function step(T) {
    for (const v of vehicles) {
      if (idle(v.prof) || v.prof.stale) continue;
      const P = predictAt(v, T, forceDegraded);
      if (!P) continue;
      predictionsTotal++;
      const pred = buildPrediction(v, P, forceDegraded);
      v.predLog.push({ issued: T, target: P.tgt.planned, pred: P.pred, truth: P.truth, lead: P.leadMin, seg: pred._seg, risk: P.risk, causes: pred.causes.map((c) => c.code) });
      while (v.predLog.length && v.predLog[0].issued < T - 2 * 3600000) v.predLog.shift();
      v.lastPred = pred;
      v.lastP = P;
      if (P.risk !== 'green') {
        const id = `${v.tr_id}-${pred.target_stop_id}`;
        const a = alerts.get(id);
        if (a) {
          a.updated_at = toIso(T);
          a.risk = P.risk;
          a.kind = P.pred >= 0 ? 'late' : 'early';
          a.prediction = pred;
        } else {
          alerts.set(id, {
            id,
            tr_id: v.tr_id,
            route_name: v.route_name,
            created_at: toIso(T),
            updated_at: toIso(T),
            risk: P.risk,
            kind: P.pred >= 0 ? 'late' : 'early',
            prediction: pred,
            status: 'active',
            actual_delay_s: null,
            error_s: null,
            _truth: P.truth,
            _target: P.tgt.planned,
          });
        }
      }
    }
    for (const [id, a] of alerts) {
      if (T >= a._target + Math.max(0, a._truth) * 1000) {
        alerts.delete(id);
        const actual = round(a._truth + gauss(a.tr_id, a._target / 1000) * 6);
        resolved.unshift({ ...a, status: 'resolved', actual_delay_s: actual, error_s: actual - a.prediction.delay_s, updated_at: toIso(T), ack_at: ackByTr.get(a.tr_id) ?? null });
      }
    }
    resolved = resolved.slice(0, 40);
  }

  function reset(warmupMin = HISTORY_MIN) {
    alerts.clear();
    ackByTr.clear();
    resolved = [];
    for (const v of vehicles) {
      v.predLog = [];
      v.lastPred = null;
      v.lastP = null;
    }
    const from = Math.floor((clock.t - Math.max(warmupMin, HISTORY_MIN) * 60000) / STEP_MS) * STEP_MS;
    for (let T = from; T <= clock.t; T += STEP_MS) step(T);
    clock.lastStep = Math.floor(clock.t / STEP_MS) * STEP_MS;
  }

  function advance() {
    const w = performance.now();
    if (clock.state === 'running') {
      const dt = (w - clock.wallAt) * clock.speed;
      clock.t += dt;
      const active = vehicles.length - 1;
      const pk = (dt / 1000 / 12) * active;
      clock.sent += pk;
      packetsTotal += pk;
    }
    clock.wallAt = w;
    let guard = 0;
    while (clock.lastStep + STEP_MS <= clock.t && guard++ < 400) {
      clock.lastStep += STEP_MS;
      step(clock.lastStep);
    }
  }

  // Следующий рейс для ТС на отстое (через 16–55 мин) и вне смены (через 3–4 ч).
  function nextTrip(v, now) {
    const minute = Math.floor(now / 60000);
    const inMin = v.prof.layover ? 16 + (39 - (minute % 40)) : 180 + (59 - (minute % 60));
    const planned = (minute + inMin) * 60000;
    return { planned: toIso(planned), stop_name: v.stops[0].name, in_min: round((planned - now) / 60000, 1) };
  }

  function vehicleState(v) {
    const now = clock.t;
    const p = v.prof;
    const base = {
      tr_id: v.tr_id,
      unit_id: v.unit_id,
      route_name: v.route_name,
      scheduled: true,
      route_m: null,
      next_trip: null,
    };
    if (idle(p)) {
      const s0 = v.stops[0];
      const trip = nextTrip(v, now);
      return {
        ...base,
        lat: s0.lat,
        lon: s0.lon,
        heading: 0,
        speed_kmh: 0,
        last_seen: toIso(now - 8000),
        gps_age_s: 8,
        status: p.layover ? 'layover' : 'out_of_service',
        next_trip: trip,
        current_delay_s: null,
        last_stop: null,
        next_stop: { stop_id: stopId(v, 0, 0), name: s0.name, planned: trip.planned, eta: trip.planned },
        prediction: null,
        risk: 'grey',
        degraded: false,
      };
    }
    const age = p.stale ? 240 + ((now / 1000) % 600) : 2 + hash01(v.tr_id, Math.floor(now / 5000)) * 10;
    const seen = now - age * 1000;
    const pos = positionAt(v, seen);
    const prev = positionAt(v, seen - 12000);
    const dist = hav(prev.lat, prev.lon, pos.lat, pos.lon);
    const speed = pos.dwell ? 0 : (dist / 12) * 3.6;
    const heading = dist > 3 ? bearing(prev.lat, prev.lon, pos.lat, pos.lon) : v.lastHeading ?? 0;
    v.lastHeading = heading;
    const c = cycleStart(v, pos.n);
    const lastPlanned = c + v.stops[pos.i].t * 1000;
    const lastDelay = delayAt(v, lastPlanned);
    const last = v.stops.length - 1;
    const nn = pos.i < last ? pos.n : pos.n + 1;
    const ni = pos.i < last ? pos.i + 1 : 0;
    const nextPlanned = cycleStart(v, nn) + v.stops[ni].t * 1000;
    const prediction = p.stale ? null : publicPrediction(v.lastPred);
    return {
      ...base,
      lat: round(pos.lat, 6),
      lon: round(pos.lon, 6),
      heading: round(heading),
      speed_kmh: round(speed, 1),
      last_seen: toIso(seen),
      gps_age_s: round(age, 1),
      status: p.stale ? 'stale' : speed > 3 ? 'moving' : 'stopped',
      current_delay_s: round(pos.delay),
      last_stop: {
        stop_id: stopId(v, pos.n, pos.i),
        name: v.stops[pos.i].name,
        planned: toIso(lastPlanned),
        actual: toIso(lastPlanned + lastDelay * 1000),
        delay_s: round(lastDelay),
      },
      next_stop: {
        stop_id: stopId(v, nn, ni),
        name: v.stops[ni].name,
        planned: toIso(nextPlanned),
        eta: toIso(nextPlanned + pos.delay * 1000),
      },
      prediction,
      risk: prediction ? prediction.risk : 'grey',
      degraded: p.stale || forceDegraded,
    };
  }

  function onlineMetrics() {
    const now = clock.t;
    let n = 0;
    let abs = 0;
    let ok = 0;
    let lead = 0;
    let total = 0;
    for (const v of vehicles) {
      for (const e of v.predLog) {
        if (e.issued < now - HISTORY_MIN * 60000) continue;
        total++;
        lead += e.lead;
        if (e.lead >= 10 && e.lead <= 15) ok++;
        if (e.target <= now) {
          n++;
          abs += Math.abs(e.pred - e.truth);
        }
      }
    }
    return {
      mae_s: n ? round(abs / n, 1) : null,
      n,
      horizon_ok_share: total ? round(ok / total, 3) : null,
      mean_lead_min: total ? round(lead / total, 1) : null,
      retro_predictions: n,
    };
  }

  function health() {
    const w = performance.now();
    let streamState = 'live';
    if (clock.state === 'paused' && w - clock.pausedWall > 8000) streamState = 'stale';
    if (clock.state === 'idle' || clock.state === 'finished') streamState = w - clock.stoppedWall > 20000 ? 'no_data' : 'stale';
    const mlState = forceDegraded ? 'fallback' : 'ok';
    const pps = clock.state === 'running' ? ((vehicles.length - 1) * clock.speed) / 12 : 0;
    return {
      status: streamState === 'live' && mlState === 'ok' ? 'ok' : 'degraded',
      stream: {
        state: streamState,
        connected_units: streamState === 'live' ? vehicles.length - 2 : 0,
        known_units: vehicles.length,
        last_packet_at: toIso(clock.t - 1000),
        stream_time: toIso(clock.t),
        packets_total: Math.round(packetsTotal),
        packets_per_s: round(pps * (0.95 + Math.random() * 0.1), 1),
        crc_errors: 0,
      },
      ml: { state: mlState, model_version: '2026-09-25-1', last_latency_ms: round(5 + Math.random() * 4, 1) },
      uptime_s: round((w - bootedAt) / 1000, 1),
    };
  }

  function activeAlerts() {
    const latest = new Map();
    const hot = new Set(vehicles.filter((v) => v.lastPred && v.lastPred.risk !== 'green' && !v.prof.stale).map((v) => v.tr_id));
    for (const a of alerts.values()) {
      if (!hot.has(a.tr_id)) continue;
      const cur = latest.get(a.tr_id);
      if (!cur || a.created_at > cur.created_at) latest.set(a.tr_id, a);
    }
    for (const tr of ackByTr.keys()) if (!hot.has(tr)) ackByTr.delete(tr);
    return [...latest.values()].map((a) => ({ ...stripAlert(a), ack_at: ackByTr.get(a.tr_id) ?? null }));
  }

  function stripAlert(a) {
    const { _truth, _target, ...rest } = a;
    return { ...rest, prediction: publicPrediction(a.prediction) };
  }

  function unscheduledState(u) {
    const src = u.near != null ? vehicles[u.near % vehicles.length] : null;
    const s0 = src ? src.stops[Math.min(2, src.stops.length - 1)] : null;
    const age = s0 ? 3 + hash01(u.tr_id, Math.floor(clock.t / 5000)) * 8 : null;
    return {
      tr_id: u.tr_id,
      unit_id: u.unit_id,
      route_name: `Без расписания (терминал ${u.unit_id})`,
      lat: s0 ? round(s0.lat + 0.0012, 6) : null,
      lon: s0 ? round(s0.lon - 0.0015, 6) : null,
      heading: s0 ? 90 : null,
      speed_kmh: s0 ? 0 : null,
      last_seen: s0 ? toIso(clock.t - age * 1000) : null,
      gps_age_s: s0 ? round(age, 1) : null,
      status: 'no_schedule',
      scheduled: false,
      route_m: null,
      next_trip: null,
      current_delay_s: null,
      last_stop: null,
      next_stop: null,
      prediction: null,
      risk: 'grey',
      degraded: false,
    };
  }

  function snapshot() {
    advance();
    const vs = vehicles.map(vehicleState);
    const ctx = UNSCHEDULED.map(unscheduledState);
    const trip = vs.filter((x) => x.status === 'moving' || x.status === 'stopped' || x.status === 'stale');
    const count = (r) => trip.filter((x) => x.risk === r).length;
    const status = (st) => vs.filter((x) => x.status === st).length;
    const m = onlineMetrics();
    return {
      stream_time: toIso(clock.t),
      generated_at: toIso(clock.t),
      summary: {
        vehicles_total: vs.length,
        vehicles_active: trip.length,
        red: count('red'),
        yellow: count('yellow'),
        green: count('green'),
        grey: count('grey'),
        layover: status('layover'),
        out_of_service: status('out_of_service'),
        offline: status('offline'),
        unscheduled: ctx.length,
        online_mae_s: m.mae_s,
        online_n: m.n,
        mean_lead_min: m.mean_lead_min,
      },
      vehicles: [...vs, ...ctx],
      alerts: activeAlerts(),
      health: health(),
    };
  }

  function routes() {
    advance();
    return vehicles.map((v) => {
      const m = v.stops.length;
      const segs = [];
      for (let i = 0; i < m - 1; i++) segs.push({ from_idx: i, to_idx: i + 1, risk: 'green' });
      const lp = v.lastPred;
      if (lp && !v.prof.stale && lp.risk !== 'green') {
        const [cn, ci] = lp._cur;
        const [tn, ti] = lp._tgt;
        let n = cn;
        let i = ci;
        let guard = 0;
        while ((n < tn || (n === tn && i < ti)) && guard++ < 400) {
          if (i < m - 1) segs[i].risk = 'yellow';
          i++;
          if (i >= m - 1) {
            i = 0;
            n++;
          }
        }
        const [a] = lp._seg;
        if (segs[a]) segs[a].risk = lp.risk;
      }
      return {
        tr_id: v.tr_id,
        route_name: v.route_name,
        stops: v.stops.map(({ stop_id, name, lat, lon }) => ({ stop_id, name, lat, lon })),
        path: v.path,
        segments: segs,
      };
    });
  }

  function hotspots(top = 15) {
    advance();
    const since = clock.t - HISTORY_MIN * 60000;
    const out = [];
    for (const v of vehicles) {
      const groups = new Map();
      for (const e of v.predLog) {
        if (e.issued < since || !e.seg) continue;
        const k = e.seg.join('-');
        let g = groups.get(k);
        if (!g) groups.set(k, (g = { seg: e.seg, n: 0, risky: 0, sum: 0, max: -Infinity, causes: new Map() }));
        g.n++;
        if (e.risk !== 'green') {
          g.risky++;
          for (const c of e.causes) g.causes.set(c, (g.causes.get(c) || 0) + 1);
        }
        g.sum += e.pred;
        g.max = Math.max(g.max, e.pred);
      }
      for (const g of groups.values()) {
        if (!g.risky || g.n < 3) continue;
        const [a, b] = g.seg;
        const s = segStops(v, a, b);
        out.push({
          tr_id: v.tr_id,
          route_name: v.route_name,
          ...s,
          n: g.n,
          risk_share: round(g.risky / g.n, 2),
          mean_delay_s: round(g.sum / g.n, 1),
          max_delay_s: round(g.max, 1),
          top_causes: [...g.causes.entries()]
            .sort((x, y) => y[1] - x[1])
            .slice(0, 3)
            .map(([code, count]) => ({ code, text: CAUSE_SHORT[code] || code, count })),
          _score: (g.risky / g.n) * Math.sqrt(g.n) * Math.max(30, Math.abs(g.sum / g.n)),
        });
      }
    }
    out.sort((x, y) => y._score - x._score);
    return out.slice(0, top).map(({ _score, ...h }) => h);
  }

  function vehicleDetail(id) {
    advance();
    const v = vehicles.find((x) => String(x.tr_id) === String(id));
    if (!v) return null;
    const st = vehicleState(v);
    const now = clock.t;
    const track = [];
    const upcoming = [];
    const history = [];
    if (!idle(v.prof)) {
      const seen = now - (st.gps_age_s || 0) * 1000;
      for (let k = 60; k >= 0; k--) {
        const t = seen - k * 30000;
        const p = positionAt(v, t);
        track.push([round(p.lon, 6), round(p.lat, 6), toIso(t)]);
      }
      const pos = positionAt(v, seen);
      for (const a of arrivals(v, cycleStart(v, pos.n), 3)) {
        if (a.n < pos.n || (a.n === pos.n && a.i <= pos.i)) continue;
        const s = v.stops[a.i];
        upcoming.push({
          stop_id: stopId(v, a.n, a.i),
          name: s.name,
          planned: toIso(a.planned),
          eta: toIso(a.planned + (pos.delay * 0.5 + delayAt(v, a.planned) * 0.5) * 1000),
          lat: s.lat,
          lon: s.lon,
        });
        if (upcoming.length >= 10) break;
      }
      const until = v.prof.stale ? seen : now;
      for (let k = HISTORY_MIN; k >= 0; k--) {
        const t = until - k * 60000;
        history.push({ t: toIso(t), delay_s: round(delayAt(v, t) + gauss(v.tr_id, t / 60000) * 5), kind: 'actual' });
      }
      v.predLog
        .filter((e, i) => e.issued >= now - HISTORY_MIN * 60000 && i % 2 === 0)
        .forEach((e) => history.push({ t: toIso(e.target), delay_s: round(e.pred), kind: 'predicted' }));
      history.sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : 0));
    } else if (st.next_trip) {
      const t0 = Date.parse(st.next_trip.planned + 'Z');
      track.push([st.lon, st.lat, st.last_seen]);
      for (const s of v.stops.slice(0, 10)) {
        const planned = toIso(t0 + s.t * 1000);
        upcoming.push({ stop_id: stopId(v, 0, v.stops.indexOf(s)), name: s.name, planned, eta: planned, lat: s.lat, lon: s.lon });
      }
    }
    return { ...st, track, upcoming, delay_history: history };
  }

  function whatif({ tr_id, scenario, value }) {
    advance();
    const v = vehicles.find((x) => String(x.tr_id) === String(tr_id));
    const base = v && !v.prof.stale ? publicPrediction(v.lastPred) : null;
    if (!base) return { base: null, delay_s_after: null, risk_after: 'grey', explanation: 'Для этого ТС сейчас нет прогноза' };
    const val = Math.max(0, Number(value) || 0);
    const k = { extra_vehicle: 1.25, dwell_cut: 0.9, signal_priority: 0.65 }[scenario] ?? 0.8;
    const eff = val * k;
    const d = base.delay_s;
    const after = d >= 0 ? Math.max(Math.min(d, -15), d - eff) : scenario === 'dwell_cut' ? d - eff * 0.4 : d + Math.min(-d, eff * 0.3);
    const sigma = (base.delay_q90_s - base.delay_q10_s) / 2.563 || 60;
    const pl = 1 - Phi((LATE_S - after) / sigma);
    const pe = Phi((EARLY_S - after) / sigma);
    const riskAfter = riskFrom(pl, pe);
    const gain = d - after;
    const where = `к остановке «${base.target_stop_name}»`;
    const texts = {
      extra_vehicle: `Резервное ТС забирает часть пассажиров, стоянки короче: ${gain >= 0 ? 'выигрыш' : 'потеря'} ${fmtDelay(Math.abs(gain)).replace('+', '')} ${where}.`,
      dwell_cut: d >= 0 ? `Сокращение стоянок на ${fmtNum(val)} с суммарно уменьшает отставание ${where}.` : `Короткие стоянки усилят опережение ${where}: сценарий не подходит для раннего ТС.`,
      signal_priority: `Приоритет на светофорах участка «${base.segment.from_stop} → ${base.segment.to_stop}» экономит около ${fmtDelay(Math.abs(gain)).replace('+', '')}.`,
    };
    return {
      base,
      delay_s_after: round(after),
      risk_after: riskAfter,
      explanation: texts[scenario] || 'Сценарий рассчитан по упрощённой модели.',
    };
  }

  function metrics() {
    advance();
    const on = onlineMetrics();
    const running = clock.state === 'running';
    const j = () => Math.random();
    return {
      online: on,
      latency: {
        ingest_to_prediction_ms_p50: round(31 + j() * 9, 1),
        ingest_to_prediction_ms_p95: round(84 + j() * 30, 1),
        ml_predict_ms_p50: round(5.2 + j() * 1.5, 1),
        ml_predict_ms_p95: round(11 + j() * 5, 1),
      },
      throughput: {
        packets_per_s: running ? round((((vehicles.length - 1) * clock.speed) / 12) * (0.95 + j() * 0.1), 1) : 0,
        queue_len: running ? Math.floor(j() * 3) : 0,
        predictions_total: predictionsTotal,
      },
      offline: { test_mae_s: 63.8, baseline_mae_s: 88.5, zero_mae_s: 131.2 },
    };
  }

  function replayerStatus() {
    advance();
    return {
      state: clock.state,
      speed: clock.speed,
      stream_time: toIso(clock.t),
      start_time: clock.startTime,
      units: vehicles.length,
      sent: Math.round(clock.sent),
      connected: clock.state === 'running' ? vehicles.length - 2 : 0,
    };
  }

  reset();

  function setAck(id, on) {
    advance();
    const a = activeAlerts().find((x) => String(x.id) === String(id));
    if (!a) return Promise.reject(new Error('HTTP 404'));
    if (on) ackByTr.set(a.tr_id, toIso(clock.t));
    else ackByTr.delete(a.tr_id);
    return later({ ...a, ack_at: on ? toIso(clock.t) : null });
  }

  const api = {
    mode: 'mock',
    reason,
    health: () => later((advance(), health())),
    state: () => later(snapshot()),
    vehicles: () => later(snapshot().vehicles),
    vehicle: (id) => {
      const d = vehicleDetail(id);
      return d ? later(d) : Promise.reject(new Error('HTTP 404'));
    },
    routes: () => later(routes(), 150),
    alerts: (status = 'active') => {
      advance();
      const act = activeAlerts();
      const res = resolved.map(stripAlert);
      return later(status === 'active' ? act : status === 'resolved' ? res : [...act, ...res]);
    },
    metrics: () => later(metrics()),
    hotspots: (top = 15) => later(hotspots(top)),
    whatif: (body) => later(whatif(body || {}), 250),
    ack: (id) => setAck(id, true),
    unack: (id) => setAck(id, false),
    replayer: {
      status: () => later(replayerStatus()),
      start: ({ speed, start_time, warmup_min } = {}) => {
        if (speed) clock.speed = +speed;
        if (start_time) {
          clock.startTime = String(start_time).slice(0, 5);
          clock.t = DAY0 + hmsToSec(clock.startTime) * 1000;
        }
        clock.state = 'running';
        clock.wallAt = performance.now();
        clock.sent = 0;
        reset(Number(warmup_min) || HISTORY_MIN);
        return later(replayerStatus());
      },
      pause: () => {
        advance();
        if (clock.state === 'running') {
          clock.state = 'paused';
          clock.pausedWall = performance.now();
        }
        return later(replayerStatus());
      },
      resume: () => {
        if (clock.state === 'paused') {
          clock.state = 'running';
          clock.wallAt = performance.now();
        }
        return later(replayerStatus());
      },
      stop: () => {
        advance();
        clock.state = 'idle';
        clock.stoppedWall = performance.now();
        return later(replayerStatus());
      },
      speed: (speed) => {
        advance();
        clock.speed = Number(speed) || clock.speed;
        return later(replayerStatus());
      },
    },
    connect: ({ onState, onLink }) => {
      const tick = () => {
        onState(structuredClone(snapshot()));
        onLink({ transport: 'mock', lastAt: Date.now(), error: null, wsOpen: false });
      };
      tick();
      const id = setInterval(tick, 1000);
      return () => clearInterval(id);
    },
  };
  return api;
}
