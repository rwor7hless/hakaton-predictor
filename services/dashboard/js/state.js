import { RISK_RANK, riskOf, isNum, IN_TRIP } from './format.js';

const subs = new Set();

export const store = {
  s: {
    api: null,
    mode: 'live',
    demoReason: null,
    backendBack: false,
    snap: null,
    link: { transport: 'connecting', lastAt: 0 },
    routes: [],
    selected: null,
    autoSelected: false,
    filter: null,
    view: 'monitor',
    leftTab: 'incidents',
    foldLeft: false,
    foldCard: false,
    ackBusy: null,
    ackErr: null,
    dialog: null,
    showCtx: false,
    resolved: [],
    resolvedErr: null,
    detail: null,
    detailErr: null,
    metrics: null,
    metricsErr: null,
    replayer: null,
    replayerErr: null,
    playerBusy: false,
    playerSpeed: 10,
    whatif: null,
    hotspots: [],
    hotspotsErr: null,
    hotspot: null,
    showUnsched: false,
    tiles: 'loading',
    tick: 0,
  },
  set(patch) {
    Object.assign(this.s, patch);
    const keys = new Set(Object.keys(patch));
    for (const fn of subs) fn(this.s, keys);
  },
  on(fn) {
    subs.add(fn);
    return () => subs.delete(fn);
  },
};

export function vehicleById(s, id) {
  if (id == null) return null;
  return (s.snap?.vehicles || []).find((v) => String(v.tr_id) === String(id)) || null;
}

export function streamTime(s) {
  return s.snap?.stream_time || s.snap?.health?.stream?.stream_time || s.replayer?.stream_time || null;
}

// Показываем текущий прогноз ТС; прогноз из инцидента — только если своего ещё нет,
// иначе после снятия риска карточка продолжала бы показывать устаревшее отклонение.
export function effectivePrediction(vehicle, alert) {
  return vehicle?.prediction || alert?.prediction || null;
}

export function incidents(s) {
  const vehicles = s.snap?.vehicles || [];
  const byVeh = new Map();
  for (const a of s.snap?.alerts || []) {
    if (!a || (a.status && a.status !== 'active')) continue;
    const k = String(a.tr_id);
    const cur = byVeh.get(k);
    const stamp = a.updated_at || a.created_at || '';
    if (!cur) byVeh.set(k, { alert: a, count: 1 });
    else {
      cur.count++;
      if (stamp > (cur.alert.updated_at || cur.alert.created_at || '')) cur.alert = a;
    }
  }
  // Закрытые за последние минуты остаются в списке с пометкой, чтобы было видно, что и почему закрылось.
  for (const a of s.snap?.recent_resolved || []) {
    const k = String(a?.tr_id);
    if (a && !byVeh.has(k)) byVeh.set(k, { alert: a, count: 0 });
  }
  for (const v of vehicles) {
    const r = riskOf(v.risk);
    const k = String(v.tr_id);
    if ((r === 'red' || r === 'yellow') && v.prediction && !byVeh.has(k)) {
      byVeh.set(k, {
        alert: {
          id: `${v.tr_id}-${v.prediction.target_stop_id ?? 'x'}`,
          tr_id: v.tr_id,
          route_name: v.route_name,
          created_at: v.prediction.issued_at,
          updated_at: v.prediction.issued_at,
          risk: r,
          kind: (v.prediction.delay_s ?? 0) < 0 ? 'early' : 'late',
          prediction: v.prediction,
          status: 'active',
          synthetic: true,
        },
        count: 0,
      });
    }
  }
  const list = [];
  for (const [k, { alert, count }] of byVeh) {
    const v = vehicles.find((x) => String(x.tr_id) === k) || null;
    const pred = effectivePrediction(v, alert);
    const liveRisk = v ? riskOf(v.risk) : null;
    // Цвет — уровень инцидента (backend держит его с гистерезисом), а не риск отдельного прогноза.
    const risk = riskOf(alert.synthetic ? liveRisk : alert.risk);
    const cleared = alert.status === 'resolved' || liveRisk === 'green';
    list.push({
      key: k,
      id: alert.id,
      alert,
      vehicle: v,
      pred,
      risk,
      kind: (pred?.delay_s ?? 0) < 0 ? 'early' : alert.kind || 'late',
      cleared,
      acked: !!alert.ack_at,
      count,
    });
  }
  // Сначала то, что ещё никто не взял в работу. Внутри группы порядок должен быть
  // устойчивым: по уровню риска и по задержке, округлённой до минуты, иначе строки
  // меняются местами каждую секунду.
  list.sort((a, b) => {
    if (a.cleared !== b.cleared) return a.cleared ? 1 : -1;
    if (a.acked !== b.acked) return a.acked ? 1 : -1;
    const r = (RISK_RANK[a.risk] ?? 9) - (RISK_RANK[b.risk] ?? 9);
    if (r) return r;
    const d = Math.round(Math.abs(b.pred?.delay_s || 0) / 60) - Math.round(Math.abs(a.pred?.delay_s || 0) / 60);
    if (d) return d;
    return a.key.localeCompare(b.key);
  });
  return list;
}

export const isScheduled = (v) => !!v && v.scheduled !== false && v.status !== 'no_schedule';

export const inTrip = (v) => !!v && IN_TRIP.has(v.status);

// Группа ТС для списков и счётчиков: в рейсе — по цвету риска, иначе по состоянию.
export function vehicleGroup(v) {
  if (!isScheduled(v)) return 'no_schedule';
  if (v.status === 'layover' || v.status === 'out_of_service' || v.status === 'offline') return v.status;
  return riskOf(v.risk);
}

export const GROUP_RANK = { red: 0, yellow: 1, green: 2, grey: 3, layover: 4, out_of_service: 5, offline: 6, no_schedule: 7 };

export function fleetCounts(s) {
  const sum = s.snap?.summary || {};
  const vs = s.snap?.vehicles || [];
  const c = { red: 0, yellow: 0, green: 0, grey: 0, layover: 0, out_of_service: 0, offline: 0, no_schedule: 0 };
  for (const v of vs) c[vehicleGroup(v)]++;
  const scheduled = vs.length - c.no_schedule;
  const fromSum = (k, fallback) => (isNum(sum[k]) ? +sum[k] : fallback);
  for (const k of ['red', 'yellow', 'green', 'grey', 'layover', 'out_of_service', 'offline']) c[k] = fromSum(k, c[k]);
  c.no_schedule = fromSum('unscheduled', c.no_schedule);
  c.total = fromSum('vehicles_total', scheduled);
  c.active = fromSum('vehicles_active', c.red + c.yellow + c.green + c.grey);
  c.noData = vs.filter((v) => isScheduled(v) && (v.status === 'stale' || v.status === 'offline'));
  return c;
}

export function sortedVehicles(s) {
  const vs = [...(s.snap?.vehicles || [])];
  const rank = (v) => GROUP_RANK[vehicleGroup(v)] ?? 9;
  const trip = (v) => (v.next_trip && isNum(v.next_trip.in_min) ? +v.next_trip.in_min : Infinity);
  vs.sort((a, b) => {
    const r = rank(a) - rank(b);
    if (r) return r;
    const t = trip(a) - trip(b);
    if (t) return t;
    return String(a.tr_id).localeCompare(String(b.tr_id), 'ru', { numeric: true });
  });
  if (s.filter === 'unacked') {
    const waiting = new Set(incidents(s).filter((x) => !x.cleared && !x.acked && !x.alert.synthetic).map((x) => x.key));
    return { main: vs.filter((v) => waiting.has(String(v.tr_id))), unscheduled: [] };
  }
  if (s.filter) return { main: vs.filter((v) => isScheduled(v) && riskOf(v.risk) === s.filter && inTrip(v)), unscheduled: [] };
  const located = (v) => (isNum(v.lat) ? 0 : 1);
  const unscheduled = vs.filter((v) => !isScheduled(v)).sort((a, b) => located(a) - located(b));
  return { main: vs.filter(isScheduled), unscheduled };
}

export const hotspotKey = (h) => `${h.tr_id}|${h.from_stop}|${h.to_stop}`;

export function hotspotByKey(s, key) {
  return key == null ? null : (s.hotspots || []).find((h) => hotspotKey(h) === key) || null;
}
