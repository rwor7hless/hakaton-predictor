import { STATUS_LABEL, riskOf, parseTs, fmtClock, fmtHM, fmtDelay, fmtNum, fmtPct, fmtProb, fmtAgo, fmtIn, esc, isNum } from './format.js';
import { incidents, vehicleById, effectivePrediction, vehicleGroup, isScheduled, inTrip } from './state.js';
import { $, setHTML, chip, ICON, GROUP_TITLE } from './dom.js';
import { todoHtml } from './inc.js';

const foldBtn = () =>
  `<button type="button" class="icon-btn" data-act="fold" data-side="card" aria-controls="card" aria-expanded="true" aria-label="Свернуть карточку" title="Свернуть карточку">${ICON.foldR}</button>`;

// Каркас карточки строится один раз на выбранное ТС; дальше обновляются только секции,
// чтобы кнопки не теряли фокус при ежесекундных обновлениях.
function ensureFrame(s) {
  const card = $('card');
  const view = s.selected == null ? 'empty' : `v:${s.selected}`;
  if (card._view === view) return;
  card._view = view;
  card._html = null;
  if (s.selected == null) {
    card.innerHTML = `<header class="panel__head card__head"><h2 class="panel__title">Карточка ТС</h2>${foldBtn()}</header><div class="card__empty"><p><b>ТС не выбрано</b></p><p>Выберите инцидент в списке или машину на карте: здесь появятся прогноз, причины и рекомендации.</p></div>`;
    return;
  }
  card.innerHTML = `
    <header class="panel__head card__head">
      <div class="card__title"><div id="c-head"></div><p class="card__status" id="c-status"></p></div>
      <span class="card__tools">${foldBtn()}<button type="button" class="icon-btn" data-act="deselect" aria-label="Закрыть карточку" title="Закрыть (Esc)">${ICON.close}</button></span>
    </header>
    <div class="card__body">
      <div id="c-todo"></div>
      <section id="c-pred" aria-live="polite"></section>
      <section class="trip" id="c-trip" aria-label="Схема рейса"></section>
      <section id="c-why"></section>
    </div>
    <footer class="card__actions" id="c-actions"></footer>`;
}

function headHtml(v) {
  const g = vehicleGroup(v);
  const badge = inTrip(v) ? chip(riskOf(v.risk)) : chip('grey', GROUP_TITLE[g]);
  const route = g === 'no_schedule' ? 'Маршрут неизвестен' : v.route_name || 'Маршрут не указан';
  return (
    `<div class="card__plate"><span class="plate">${esc(v.tr_id)}</span>${badge}${v.degraded ? '<span class="chip chip--muted" title="Прогноз строит резервная эвристика или GPS устарел">упрощённый режим</span>' : ''}</div>` +
    `<p class="card__route">${esc(route)}</p>`
  );
}

function statusText(v) {
  const g = vehicleGroup(v);
  const facts = [];
  if (inTrip(v)) facts.push(STATUS_LABEL[v.status] || v.status || '');
  if (isNum(v.speed_kmh) && v.status === 'moving') facts.push(`${fmtNum(v.speed_kmh)} км/ч`);
  if (isNum(v.gps_age_s)) facts.push(`GPS ${fmtAgo(v.gps_age_s)} назад`);
  else if (!isNum(v.lat) && g !== 'no_schedule') facts.push('координаты не поступают');
  if (v.unit_id != null) facts.push(`блок ${v.unit_id}`);
  const t = facts.join(', ');
  return t ? t[0].toUpperCase() + t.slice(1) : '';
}

function idleHtml(v) {
  const g = vehicleGroup(v);
  const t = v.next_trip;
  if (g === 'no_schedule') {
    return `<div class="idle"><p>У этого ТС нет расписания в системе. Оно показано для контекста, прогноз не строится.</p>${isNum(v.lat) ? '' : '<p class="idle__sub">Координаты от бортового блока сейчас не поступают, поэтому на карте его нет.</p>'}</div>`;
  }
  const where = t?.stop_name ? `«${esc(t.stop_name)}»` : '';
  const text =
    g === 'layover'
      ? `На отстое${where ? ` у ${where}` : ''}. Прогноз появится за 15 минут до отправления.`
      : t
        ? `Вне смены.${where ? ` Рейс начнётся от ${where}.` : ''} Прогноз появится за 15 минут до отправления.`
        : 'Вне смены: рейсов по расписанию в ближайшие часы нет, прогноз не строится.';
  const time = t?.planned ? `<div class="idle__time"><span>Следующий рейс</span><b>${fmtHM(t.planned)}</b>${isNum(t.in_min) ? `<span>через ${fmtIn(t.in_min)}</span>` : ''}</div>` : '';
  return `<div class="idle">${time}<p>${text}</p></div>`;
}

function noPredictionText(v) {
  if (v.status === 'stale') return `Прогноза нет: GPS не обновлялся ${fmtAgo(v.gps_age_s)}. На карте последнее известное положение.`;
  if (v.status === 'offline') return 'Прогноза нет: бортовой блок не на связи. Прогноз вернётся, когда придут свежие координаты.';
  return 'Прогноз ещё не рассчитан: ждём достаточно телеметрии.';
}

function predHtml(v, p) {
  if (!p && !inTrip(v) && v.status !== 'offline') return idleHtml(v);
  if (!p) return `<div class="idle"><p>${esc(noPredictionText(v))}</p></div>`;
  const risk = riskOf(p.risk);
  const early = (+p.delay_s || 0) < 0;
  const prob = early ? p.p_early : p.p_late;
  const tp = parseTs(p.target_planned);
  const expected = Number.isFinite(tp) && isNum(p.delay_s) ? fmtClock(tp + p.delay_s * 1000) : null;
  const currentLabel = !isNum(v.current_delay_s)
    ? 'Сейчас нет данных об отклонении'
    : +v.current_delay_s > 0 ? 'Сейчас отстаёт от расписания'
      : +v.current_delay_s < 0 ? 'Сейчас опережает расписание' : 'Сейчас по расписанию';
  const lead = isNum(p.lead_min) ? +p.lead_min : null;
  const inWindow = lead != null && lead >= 10 && lead <= 15;
  const seg = p.segment;
  // В окне 10–15 мин нет плановой остановки: показываем последний прогноз, он в силе до своей цели.
  const h = v.prediction_held;
  const held = h
    ? `<p class="held">Новых прогнозов нет: ${h.reason === 'trip_end' ? 'до конечной меньше 10 мин' : 'в ближайшие 10–15 мин по расписанию нет остановок (длинный перегон)'}. Показан прогноз от ${fmtClock(p.issued_at).slice(0, 5)}, он в силе до прибытия на «${esc(p.target_stop_name || '—')}»${h.resume_at ? `; следующий — около ${fmtHM(h.resume_at)}` : ''}.</p>`
    : '';
  return (
    held +
    `<div class="hero hero--${risk}">` +
    `<b class="hero__v">${fmtDelay(p.delay_s)}</b>` +
    `<p class="hero__t">${early ? 'опережение' : 'опоздание'} к остановке <b>«${esc(p.target_stop_name || '—')}»</b><span>план ${fmtHM(p.target_planned)}${expected ? `, ожидается ${expected}` : ''}</span></p>` +
    `</div>` +
    `<dl class="mgrid">` +
    `<div><dt>${early ? 'Вероятность опережения' : 'Вероятность опоздания'}</dt><dd>${fmtProb(prob)}</dd></div>` +
    `<div><dt>Интервал 10–90 %</dt><dd>${fmtDelay(p.delay_q10_s)} … ${fmtDelay(p.delay_q90_s)}</dd></div>` +
    `<div><dt>${currentLabel}</dt><dd>${fmtDelay(v.current_delay_s)}</dd></div>` +
    `<div${lead != null && !inWindow ? ' class="is-off" title="Упреждение вне целевого окна 10–15 мин: у ближайших остановок нет планового времени в этом окне"' : ''}><dt>Упреждение</dt><dd>${lead != null ? `${fmtNum(lead, lead % 1 ? 1 : 0)} мин` : '—'}</dd><dd class="mgrid__sub">прогноз от ${fmtClock(p.issued_at)}</dd></div>` +
    `</dl>` +
    (seg
      ? `<div class="segline"><span class="segline__l">Участок</span><span class="segline__v">${esc(seg.from_stop || '—')} ${ICON.arrow} ${esc(seg.to_stop || '—')}</span></div>`
      : '')
  );
}

// Первая рекомендация уже стоит в блоке «Что делать», если он показан.
function whyHtml(p, skipFirst) {
  if (!p) return '';
  const causes = (p.causes || []).slice().sort((a, b) => (+b.weight || 0) - (+a.weight || 0));
  const recs = (p.recommendations || []).slice(skipFirst ? 1 : 0);
  return (
    (causes.length
      ? `<h3 class="card__h">Вероятные причины</h3><ul class="causes">${causes
          .map((c) => {
            const w = Math.max(0, Math.min(1, +c.weight || 0));
            return `<li><span class="causes__t">${esc(c.text || c.code)}</span><span class="causes__bar" aria-hidden="true"><i style="width:${(w * 100).toFixed(0)}%"></i></span><span class="causes__w" title="Вес причины">${fmtPct(w)}</span></li>`;
          })
          .join('')}</ul>`
      : '') +
    (recs.length ? `<h3 class="card__h">${skipFirst ? 'Ещё рекомендации' : 'Рекомендации диспетчеру'}</h3><ul class="recs">${recs.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : '')
  );
}

// «Взять в работу» живёт в блоке «Что делать»; внизу — инструменты разбора.
function actionsHtml(s, v, p) {
  const out = [];
  if (p) out.push('<button type="button" class="btn" data-act="dialog" data-dialog="dlg-whatif" aria-haspopup="dialog">Что если…</button>');
  if (isScheduled(v)) out.push('<button type="button" class="btn" data-act="dialog" data-dialog="dlg-review" aria-haspopup="dialog">Разбор</button>');
  return out.length ? `<div class="card__btns card__btns--${out.length}">${out.join('')}</div>` : '';
}

export function renderCard(s) {
  ensureFrame(s);
  if (s.selected == null) return;
  const v = vehicleById(s, s.selected) || (s.detail && String(s.detail.tr_id) === String(s.selected) ? s.detail : null);
  if (!v) {
    setHTML($('c-head'), `<div class="card__plate"><span class="plate">${esc(s.selected)}</span></div>`);
    setHTML($('c-pred'), '<p class="empty">Этого ТС нет в текущем состоянии потока.</p>');
    setHTML($('c-status'), '');
    setHTML($('c-todo'), '');
    setHTML($('c-why'), '');
    setHTML($('c-actions'), '');
    return;
  }
  const it = incidents(s).find((x) => x.key === String(v.tr_id));
  const p = effectivePrediction(v, it?.alert);
  setHTML($('c-head'), headHtml(v));
  setHTML($('c-status'), esc(statusText(v)));
  const todo = todoHtml(s, p, it);
  setHTML($('c-todo'), todo);
  setHTML($('c-pred'), predHtml(v, p));
  setHTML($('c-why'), whyHtml(p, !!todo && !!p?.recommendations?.length));
  setHTML($('c-actions'), actionsHtml(s, v, p));
}
