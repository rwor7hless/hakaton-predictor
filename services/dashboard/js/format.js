export const MINUS = '−';
const NNBSP = ' ';

const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

export const RISKS = ['red', 'yellow', 'green', 'grey'];
export const RISK_LABEL = { red: 'Критично', yellow: 'Внимание', green: 'По графику', grey: 'Без прогноза' };
export const RISK_RANK = { red: 0, yellow: 1, green: 2, grey: 3 };

export const STATUS_LABEL = {
  moving: 'в движении',
  stopped: 'стоит',
  stale: 'нет свежих данных',
  offline: 'нет связи',
  layover: 'на отстое',
  out_of_service: 'вне смены',
  no_schedule: 'нет расписания',
};

export const IN_TRIP = new Set(['moving', 'stopped', 'stale']);

// Семантика сценариев — backend/whatif.py.
export const SCENARIOS = {
  extra_vehicle: { label: 'Выпустить резервное ТС', hint: 'Рейс выполняет машина с конечной, которая идёт по графику.' },
  dwell_cut: { label: 'Сократить стоянку', hint: 'Посадка через все двери или регулировщик на остановке.' },
  signal_priority: { label: 'Приоритет на светофорах', hint: 'Время хода до цели прогноза сокращается.' },
};

export function riskOf(v) {
  return RISK_LABEL[v] ? v : 'grey';
}

const pad = (n) => String(n).padStart(2, '0');

export const isNum = (x) => x !== null && x !== undefined && x !== '' && typeof x !== 'boolean' && Number.isFinite(+x);

export const isLatLon = (lat, lon) => isNum(lat) && isNum(lon) && Math.abs(+lat) <= 90 && Math.abs(+lon) <= 180 && !(+lat === 0 && +lon === 0);

export function parseTs(v) {
  if (v == null || v === '') return NaN;
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?/.exec(String(v));
  if (!m) return NaN;
  const ms = m[7] ? Number((m[7] + '00').slice(0, 3)) : 0;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0), ms);
}

export function toIso(ms) {
  return new Date(ms).toISOString().slice(0, 19);
}

export function fmtClock(v) {
  const t = parseTs(v);
  if (!Number.isFinite(t)) return '--:--:--';
  const d = new Date(t);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

export function fmtHM(v) {
  const t = parseTs(v);
  if (!Number.isFinite(t)) return '--:--';
  const d = new Date(t);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export function fmtDate(v) {
  const t = parseTs(v);
  if (!Number.isFinite(t)) return '';
  const d = new Date(t);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

function mss(abs) {
  const s = Math.round(abs);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

export function fmtDelay(s) {
  if (s == null || !Number.isFinite(+s)) return '—';
  const v = Math.round(+s);
  if (v === 0) return '0:00';
  return (v > 0 ? '+' : MINUS) + mss(Math.abs(v));
}

export function fmtAbsDur(s) {
  if (s == null || !Number.isFinite(+s)) return '—';
  return mss(Math.abs(+s));
}

export function fmtNum(x, digits = 0) {
  if (x == null || !Number.isFinite(+x)) return '—';
  return (+x).toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtPct(p, digits = 0) {
  if (p == null || !Number.isFinite(+p)) return '—';
  return fmtNum(+p * 100, digits) + NNBSP + '%';
}

export function fmtProb(p) {
  if (p == null || !Number.isFinite(+p)) return '—';
  if (+p >= 0.995) return '>99' + NNBSP + '%';
  if (+p > 0 && +p < 0.005) return '<1' + NNBSP + '%';
  return fmtPct(p);
}

export function fmtMin(m, digits = 0) {
  if (m == null || !Number.isFinite(+m)) return '—';
  return fmtNum(m, digits) + NNBSP + 'мин';
}

export function fmtIn(min) {
  if (min == null || !Number.isFinite(+min)) return '';
  const m = Math.max(0, Math.round(+min));
  if (m < 60) return `${m}${NNBSP}мин`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h}${NNBSP}ч ${r}${NNBSP}мин` : `${h}${NNBSP}ч`;
}

export function fmtAgo(sec) {
  if (sec == null || !Number.isFinite(sec)) return '';
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${s}${NNBSP}с`;
  if (s < 3600) return `${Math.floor(s / 60)}${NNBSP}мин ${pad(s % 60)}${NNBSP}с`;
  return `${Math.floor(s / 3600)}${NNBSP}ч ${pad(Math.floor((s % 3600) / 60))}${NNBSP}мин`;
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

export function hmsToSec(hms) {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?/.exec(String(hms || ''));
  if (!m) return NaN;
  return +m[1] * 3600 + +m[2] * 60 + +(m[3] || 0);
}
