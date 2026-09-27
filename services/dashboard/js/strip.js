import { riskOf, fmtHM, fmtDelay, fmtNum, esc, isNum } from './format.js';
import { incidents, vehicleById, effectivePrediction, vehicleGroup, inTrip } from './state.js';
import { $, setHTML, plural } from './dom.js';

// Схема рейса выбранного ТС в карточке: пройденная остановка, машина, ближайшие
// остановки по расписанию и цель прогноза — сверху вниз. Машина стоит между последней
// пройденной и следующей остановкой: точную долю перегона не показываем, потому что
// остановки расписания не сопоставляются с метражом маршрута однозначно.

const AHEAD_MAX = 5;

function compress(nodes) {
  if (nodes.length <= AHEAD_MAX) return nodes;
  const head = nodes.slice(0, AHEAD_MAX - 2);
  return [...head, { kind: 'gap', n: nodes.length - head.length - 1 }, nodes[nodes.length - 1]];
}

function rowHtml(n) {
  if (n.kind === 'gap') {
    return `<li class="vs vs--gap"><i class="vs__dot" aria-hidden="true"></i><span class="vs__name">ещё ${fmtNum(n.n)} ${plural(n.n, 'остановка', 'остановки', 'остановок')}</span></li>`;
  }
  if (n.kind === 'veh') {
    return (
      `<li class="vs vs--veh${n.idle ? ' vs--idle' : ''}"><i class="vs__dot" aria-hidden="true"></i>` +
      `<span class="vs__name">${esc(n.text)}</span>` +
      (isNum(n.dev) ? `<span class="vs__dev" title="Отклонение от графика сейчас">${fmtDelay(n.dev)}</span>` : '') +
      `</li>`
    );
  }
  let time = `<span class="vs__time">${fmtHM(n.time)}</span>`;
  let dev = '';
  if (n.kind === 'passed') {
    time = `<span class="vs__time">${fmtHM(n.time)}<small>факт</small></span>`;
    if (isNum(n.dev)) dev = `<span class="vs__dev" title="Отклонение на этой остановке">${fmtDelay(n.dev)}</span>`;
  } else if (n.kind === 'target') {
    time = `<span class="vs__time">${fmtHM(n.time)}<small>план</small></span>`;
    dev = `<span class="vs__dev vs__dev--${n.risk}" title="Прогноз модели к этой остановке">${fmtDelay(n.dev)}</span>`;
  }
  return (
    `<li class="vs vs--${n.kind}"><i class="vs__dot" aria-hidden="true"></i>` +
    `<span class="vs__name" title="${esc(n.name)}">${esc(n.name)}${n.kind === 'target' ? '<small>цель прогноза</small>' : ''}</span>` +
    `${time}${dev}</li>`
  );
}

function sectionHtml(extra, body) {
  return `<h3 class="card__h trip__h">Схема рейса${extra ? `<span>${extra}</span>` : ''}</h3>${body}`;
}

export function renderStrip(s) {
  const el = $('c-trip');
  if (!el) return;
  const v = s.selected != null ? vehicleById(s, s.selected) : null;
  const g = v ? vehicleGroup(v) : null;
  if (!v || g === 'no_schedule') {
    setHTML(el, '');
    return;
  }
  const detail = s.detail && String(s.detail.tr_id) === String(v.tr_id) ? s.detail : null;
  if (!detail) {
    setHTML(el, sectionHtml('', `<p class="trip__msg">${s.detailErr ? esc(s.detailErr) : 'Загружаем остановки…'}</p>`));
    return;
  }
  const up = detail.upcoming || [];

  if (!inTrip(v)) {
    if (!up.length) {
      setHTML(el, sectionHtml('', '<p class="trip__msg">Рейсов по расписанию в ближайшие часы нет.</p>'));
      return;
    }
    const rows = [{ kind: 'veh', idle: true, text: g === 'layover' ? 'ТС на отстое' : 'ТС вне смены' }, ...compress(up.map((u) => ({ kind: 'next', name: u.name || u.stop_id, time: u.planned })))];
    const extra = v.next_trip?.planned ? `рейс в ${fmtHM(v.next_trip.planned)}` : '';
    setHTML(el, sectionHtml(extra, `<ol class="vrail vrail--grey">${rows.map(rowHtml).join('')}</ol>`));
    return;
  }

  const it = incidents(s).find((x) => x.key === String(v.tr_id));
  const p = effectivePrediction(v, it?.alert);
  const risk = p ? riskOf(p.risk) : 'grey';
  const ti = p ? up.findIndex((u) => String(u.stop_id) === String(p.target_stop_id)) : -1;
  const ahead = (ti >= 0 ? up.slice(0, ti + 1) : up.slice(0, AHEAD_MAX - 1)).map((u, i) => ({
    kind: i === ti ? 'target' : 'next',
    name: u.name || u.stop_id,
    time: u.planned,
    dev: i === ti ? p.delay_s : null,
    risk,
  }));
  const rows = [];
  if (v.last_stop) rows.push({ kind: 'passed', name: v.last_stop.name || v.last_stop.stop_id, time: v.last_stop.actual, dev: v.last_stop.delay_s });
  rows.push({ kind: 'veh', text: 'ТС сейчас', dev: v.current_delay_s });
  rows.push(...compress(ahead));

  const extra = ti >= 0 ? `до цели ${fmtNum(ti + 1)} ${plural(ti + 1, 'остановка', 'остановки', 'остановок')}` : p ? 'цель дальше ближайших остановок' : 'прогноз ещё не готов';
  setHTML(el, sectionHtml(extra, `<ol class="vrail vrail--${risk}">${rows.map(rowHtml).join('')}</ol>`));
}
