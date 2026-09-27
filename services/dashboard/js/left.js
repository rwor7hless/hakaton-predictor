import { RISK_LABEL, parseTs, fmtHM, fmtDelay, fmtProb, fmtAgo, fmtNum, esc, isNum } from './format.js';
import { fleetCounts, incidents, sortedVehicles, vehicleGroup, inTrip } from './state.js';
import { $, setHTML, patchButtons, GROUP_TITLE } from './dom.js';

const CLOSED_WHY = { trip_end: 'рейс завершён', no_data: 'нет данных от ТС', no_forecast: 'прогноз больше не строится' };

function incidentRow(it, s) {
  const p = it.pred || {};
  const early = it.kind === 'early';
  const prob = early ? p.p_early : p.p_late;
  const sel = String(s.selected) === it.key;
  const tags = [];
  const closed = it.alert.status === 'resolved';
  if (it.cleared) tags.push(`<span class="tag">${closed ? 'закрыт' : 'риск снят'}</span>`);
  else if (it.acked) tags.push(`<span class="tag tag--work" title="Взят в работу в ${fmtHM(it.alert.ack_at)}">в работе</span>`);
  const opened = it.alert.created_at && !it.alert.synthetic && !closed ? `<span class="ev__time" title="Инцидент открыт">с ${fmtHM(it.alert.created_at)}</span>` : '';
  const target = `${early ? 'опережение' : 'опоздание'} к «${esc(p.target_stop_name || '—')}»`;
  const planned = parseTs(p.target_planned);
  const expected = Number.isFinite(planned) && isNum(p.delay_s) ? fmtHM(planned + Number(p.delay_s) * 1000) : '';
  const cl = it.alert.closing;
  const calm = !closed && cl && cl.calm_s > 0;
  const when = closed
    ? `закрыт в ${fmtHM(it.alert.resolved_at)}: ${CLOSED_WHY[it.alert.resolved_reason] || `прогноз ${Math.round((cl?.need_s ?? 180) / 60)} мин в норме`}`
    : calm
      ? `прогноз в норме ${fmtAgo(cl.calm_s)} из ${Math.round(cl.need_s / 60)} мин — закроется сам`
      : '';
  return {
    key: it.key,
    cls: `ev ev--${it.risk}${sel ? ' is-sel' : ''}${it.cleared ? ' is-cleared' : ''}${it.acked ? ' is-acked' : ''}`,
    attrs: { 'data-act': 'select', 'data-id': it.key, 'aria-current': sel ? 'true' : null },
    html:
      `<span class="ev__top"><i class="dot dot--${it.risk}" aria-hidden="true"></i><b class="ev__id">ТС ${esc(it.key)}</b>${it.cleared ? '' : `<span class="sr">Уровень риска: ${RISK_LABEL[it.risk] || 'не определён'}</span>`}${tags.join('')}${opened}</span>` +
      `<span class="ev__main"><b class="ev__delay">${fmtDelay(p.delay_s)}</b><span class="ev__target">${target}${when ? `<span>${esc(when)}</span>` : ''}</span></span>` +
      (expected || prob != null ? `<span class="ev__metrics">${expected ? `<span class="ev__metric"><small>Ожидается</small><b>${expected}</b></span>` : ''}${prob != null ? `<span class="ev__metric"><small>Вероятность</small><b>${fmtProb(prob)}</b></span>` : ''}</span>` : ''),
  };
}

function vehicleNote(v) {
  const g = vehicleGroup(v);
  const p = v.prediction;
  const tripAt = v?.next_trip?.planned ? fmtHM(v.next_trip.planned) : null;
  if (g === 'no_schedule') return isNum(v.gps_age_s) ? `GPS ${fmtAgo(v.gps_age_s)} назад` : 'нет координат';
  if (g === 'layover') return tripAt ? `на отстое, рейс в ${tripAt}` : 'на отстое';
  if (g === 'out_of_service') return tripAt ? `вне смены, рейс в ${tripAt}` : 'вне смены';
  if (g === 'offline') return 'нет связи с бортовым блоком';
  if (p) return `прогноз ${fmtDelay(p.delay_s)} к «${p.target_stop_name || '—'}»${v.status === 'stale' ? ', данные устарели' : ''}`;
  return v.status === 'stale' ? 'нет свежих данных, прогноза нет' : 'в рейсе, прогноз ещё не готов';
}

function vehicleRow(v, s) {
  const g = vehicleGroup(v);
  const sel = String(s.selected) === String(v.tr_id);
  const ctx = g === 'no_schedule';
  const route = ctx ? String(v.route_name || '').replace(/^Без расписания\s*\((.*)\)$/, '$1') : v.route_name || '';
  const now = inTrip(v) && isNum(v.current_delay_s) ? fmtDelay(v.current_delay_s) : '';
  return {
    key: String(v.tr_id),
    cls: `vr vr--${g}${sel ? ' is-sel' : ''}`,
    attrs: { 'data-act': 'select', 'data-id': String(v.tr_id), 'aria-current': sel ? 'true' : null, title: GROUP_TITLE[g] || null },
    html:
      `<i class="vr__sw" aria-hidden="true"></i><span class="vr__id">${esc(v.tr_id)}</span><span class="vr__route">${esc(route)}</span>` +
      `<span class="vr__now"${now ? ' title="Отклонение от графика сейчас"' : ''}>${now}</span><span class="vr__note">${esc(vehicleNote(v))}</span>`,
  };
}

export function renderLeft(s) {
  const all = incidents(s);
  const waiting = all.filter((x) => !x.cleared && !x.acked && !x.alert.synthetic);
  const list = s.filter === 'unacked' ? waiting : s.filter ? all.filter((x) => x.risk === s.filter) : all;
  const active = all.filter((x) => !x.cleared).length;
  const counts = fleetCounts(s);
  const { main, unscheduled } = sortedVehicles(s);
  const tab = s.leftTab;

  setHTML(
    $('left-tabs'),
    `<button type="button" role="tab" data-act="left-tab" data-tab="incidents" aria-selected="${tab === 'incidents'}">Инциденты <b>${fmtNum(active)}</b></button>` +
      `<button type="button" role="tab" data-act="left-tab" data-tab="vehicles" aria-selected="${tab === 'vehicles'}">Все ТС <b>${fmtNum(counts.total)}</b></button>`,
  );

  setHTML($('left-filters'), [
    ['red', counts.red, 'Критично'],
    ['yellow', counts.yellow, 'Внимание'],
    ['unacked', waiting.length, 'Не взяты'],
  ].map(([key, count, label]) => `<button type="button" class="ifilter ifilter--${key}" data-act="filter" data-risk="${key}" aria-pressed="${s.filter === key}" title="${s.filter === key ? 'Сбросить фильтр' : `Показать только: ${label.toLowerCase()}`}"><b>${fmtNum(count)}</b><span>${label}</span></button>`).join(''));

  const inc = $('inc-list');
  const veh = $('veh-list');
  const more = $('veh-more');
  const ctx = $('veh-ctx');
  inc.hidden = tab !== 'incidents';
  veh.hidden = tab !== 'vehicles';

  let emptyText = '';
  if (tab === 'incidents') {
    patchButtons(inc, list.map((it) => incidentRow(it, s)));
    if (!list.length) emptyText = !s.snap ? 'Ждём данные от сервера.' : s.filter === 'unacked' ? 'Невзятых в работу инцидентов нет.' : s.filter ? `Инцидентов «${RISK_LABEL[s.filter]}» нет.` : 'Активных инцидентов нет.';
    more.hidden = true;
    ctx.hidden = true;
  } else {
    patchButtons(veh, main.map((v) => vehicleRow(v, s)));
    if (!main.length) emptyText = !s.snap ? 'Ждём данные от сервера.' : s.filter === 'unacked' ? 'Нет ТС с невзятыми инцидентами.' : s.filter ? `Сейчас нет ТС «${RISK_LABEL[s.filter]}».` : 'ТС с расписанием в потоке пока нет.';
    const open = !!s.showUnsched && unscheduled.length > 0;
    more.hidden = !unscheduled.length;
    more.setAttribute('aria-expanded', String(open));
    setHTML(more, open ? 'Скрыть ТС без расписания' : `Показать ${fmtNum(unscheduled.length)} ТС без расписания`);
    ctx.hidden = !open;
    patchButtons(ctx, open ? unscheduled.map((v) => vehicleRow(v, s)) : []);
  }
  const empty = $('left-empty');
  setHTML(empty, emptyText);
  empty.hidden = !emptyText;
}
