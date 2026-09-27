import { SCENARIOS, RISK_LABEL, riskOf, parseTs, fmtDate, fmtClock, fmtHM, fmtDelay, fmtAbsDur, fmtNum, esc, isNum } from './format.js';
import { incidents, vehicleById, effectivePrediction, streamTime, inTrip } from './state.js';
import { $, setHTML, chip, ICON } from './dom.js';
import { renderDelayChart } from './chart.js';
import { CLOSED, reasonText } from './inc.js';

// ---------- поток ----------

const PLAYER_STATE = { running: 'идёт', paused: 'на паузе', idle: 'остановлен', finished: 'проигран до конца' };

export function renderStream(s) {
  const r = s.replayer;
  const st = r?.state;
  const speed = (st === 'running' || st === 'paused') && r?.speed ? r.speed : s.playerSpeed;
  document.querySelectorAll('#player [data-act="speed"]').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.speed === +speed)));
  const busy = s.playerBusy;
  const unavailable = !r && s.replayerErr;
  const live = st === 'running' || st === 'paused';
  const pause = $('pl-pause');
  pause.disabled = busy || !live;
  pause.dataset.act = st === 'paused' ? 'resume' : 'pause';
  pause.textContent = st === 'paused' ? 'Продолжить' : 'Пауза';
  $('pl-stop').disabled = busy || !live;
  const start = $('pl-start');
  start.disabled = busy || !!unavailable;
  start.textContent = live ? 'Перезапустить' : 'Запустить';
  const t = $('pl-time');
  if (r?.start_time && !t._touched && document.activeElement !== t) t.value = String(r.start_time).slice(0, 5);

  const rows = [];
  if (unavailable) rows.push(['Проигрыватель', `<span class="t-red">недоступен: ${esc(s.replayerErr)}</span>`]);
  else if (r) {
    rows.push(['Состояние', `${PLAYER_STATE[st] || esc(st || '—')}${speed ? `, ×${esc(speed)}` : ''}${r.phase === 'warmup' ? ', прогрев истории' : ''}`]);
    rows.push(['Время потока', fmtClock(r.stream_time)]);
    if (isNum(r.sent)) rows.push(['Отправлено пакетов', `${fmtNum(r.sent)}${isNum(r.total) ? ` из ${fmtNum(r.total)}` : ''}`]);
    if (isNum(r.units)) rows.push(['Бортовых блоков на связи', `${fmtNum(r.connected ?? 0)} из ${fmtNum(r.units)}`]);
    if (r.error) rows.push(['Ошибка', `<span class="t-red">${esc(r.error)}</span>`]);
  } else rows.push(['Состояние', 'запрашиваем…']);
  setHTML($('pl-state'), rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join(''));
}

// ---------- что если ----------

// Параметр у каждого сценария свой: backend читает value как секунды для стоянки
// и как проценты времени хода для светофоров; резервному ТС параметр не нужен.
export const WHATIF_PARAM = {
  extra_vehicle: null,
  dwell_cut: { label: 'Сократить простой на', unit: 'с', min: 15, max: 180, step: 15, value: 60 },
  signal_priority: { label: 'Сократить время хода до цели на', unit: '%', min: 5, max: 30, step: 5, value: 15 },
};

function paramHtml(sc) {
  const p = WHATIF_PARAM[sc];
  if (!p) return '<p class="wi__noparam">Параметр не нужен: рейс выполняет машина, выпущенная с конечной по графику.</p>';
  return (
    `<label class="wi__param" for="wi-v"><span>${p.label}</span>` +
    `<input type="range" id="wi-v" name="value" min="${p.min}" max="${p.max}" step="${p.step}" value="${p.value}">` +
    `<output id="wi-o" for="wi-v">${p.value} ${p.unit}</output></label>`
  );
}

function whatifResult(s, p) {
  const w = s.whatif;
  if (!w || String(w.tr_id) !== String(s.selected)) return '';
  if (w.loading) return '<p class="muted">Считаем…</p>';
  if (w.error) return `<p class="err">Не удалось рассчитать сценарий: ${esc(w.error)}</p>`;
  const r = w.res || {};
  const base = r.base || p;
  const rb = riskOf(base.risk);
  const ra = riskOf(r.risk_after);
  const diff = isNum(base.delay_s) && isNum(r.delay_s_after) ? Math.abs(base.delay_s) - Math.abs(r.delay_s_after) : null;
  const param = WHATIF_PARAM[w.scenario];
  const what = `${esc(SCENARIOS[w.scenario]?.label || w.scenario)}${param ? ` (${fmtNum(w.value)} ${param.unit})` : ''}`;
  return (
    `<div class="wr"><div class="wr__col"><span>без меры</span><b class="t-${rb}">${fmtDelay(base.delay_s)}</b>${chip(rb)}</div>` +
    `<span class="wr__arrow">${ICON.arrow}</span>` +
    `<div class="wr__col"><span>с мерой</span><b class="t-${ra}">${fmtDelay(r.delay_s_after)}</b>${chip(ra)}</div></div>` +
    (diff != null
      ? `<p class="wr__gain">${what}: ${diff > 0.5 ? `отклонение от графика меньше на ${fmtAbsDur(diff)}` : 'прогноз почти не меняется'}.${base.issued_at ? ` Расчёт по прогнозу от ${fmtClock(base.issued_at)}.` : ''}</p>`
      : '')
  );
}

export function renderWhatif(s) {
  const body = $('wi-body');
  const v = vehicleById(s, s.selected);
  const it = v ? incidents(s).find((x) => x.key === String(v.tr_id)) : null;
  const p = v ? effectivePrediction(v, it?.alert) : null;
  const key = v && p ? `v:${v.tr_id}` : 'none';
  if (body._key !== key) {
    body._key = key;
    body.innerHTML = !p
      ? '<p class="empty">Для расчёта нужен текущий прогноз по ТС.</p>'
      : `<p class="dlg__lead" id="wi-lead"></p>
        <form id="wi-form" class="wi">
          <fieldset class="wi__opts"><legend class="sr">Мера</legend>
            ${Object.entries(SCENARIOS)
              .map(([k, sc], i) => `<label class="opt"><input type="radio" name="sc" value="${k}"${i === 0 ? ' checked' : ''}><span class="opt__t">${esc(sc.label)}</span><span class="opt__h">${esc(sc.hint)}</span></label>`)
              .join('')}
          </fieldset>
          <div id="wi-param">${paramHtml('extra_vehicle')}</div>
          <button class="btn btn--primary" type="submit" id="wi-go">Рассчитать</button>
        </form>
        <div id="wi-res" aria-live="polite"></div>`;
  }
  if (!p) return;
  setHTML($('wi-lead'), `ТС ${esc(v.tr_id)}: прогноз <b>${fmtDelay(p.delay_s)}</b> к «${esc(p.target_stop_name || '—')}» в ${fmtHM(p.target_planned)}. Модель пересчитает прогноз с учётом выбранной меры.`);
  setHTML($('wi-res'), whatifResult(s, p));
  $('wi-go').disabled = !!s.whatif?.loading;
}

export function setWhatifScenario(sc) {
  setHTML($('wi-param'), paramHtml(sc));
}

// ---------- разбор ----------

export function incidentHistoryHtml(a) {
  const opened = a.opened || {};
  const openedAt = opened.at || a.created_at;
  const events = [];
  const add = (at, title, detail) => {
    if (Number.isFinite(parseTs(at))) events.push({ at, title, detail });
  };
  add(openedAt, 'Открыт', opened.reason
    ? `Прогноз${opened.target_stop_name ? ` к «${opened.target_stop_name}»` : ''}: ${reasonText(opened.reason)}`
    : 'Прогноз показал риск отклонения от графика');

  let previousRisk = riskOf(opened.reason?.risk || a.history?.[0]?.[2] || a.risk);
  for (const [at, delay, rawRisk] of a.history || []) {
    const risk = riskOf(rawRisk);
    if (risk !== previousRisk && isNum(delay)) {
      const title = risk === 'green' ? 'Прогноз вне зоны риска' : risk === 'grey' ? 'Прогноз недоступен' : `Прогноз: «${RISK_LABEL[risk]}»`;
      add(at, title, fmtDelay(delay));
    }
    previousRisk = risk;
  }
  if (a.reopened_at) add(a.reopened_at, 'Возобновлён', 'Риск вернулся после закрытия');
  if (a.ack_at) add(a.ack_at, 'Взят в работу', 'Диспетчер принял инцидент');
  if (a.status === 'resolved') add(a.resolved_at, 'Закрыт', CLOSED[a.resolved_reason] || 'риск снят');
  else if (isNum(a.prediction?.delay_s) && !events.some((e) => e.at === a.updated_at)) {
    add(a.updated_at, 'Последний прогноз', fmtDelay(a.prediction.delay_s));
  }
  events.sort((x, y) => parseTs(x.at) - parseTs(y.at));
  return `<ol class="rv-history">${events.map((e) => `<li><time datetime="${esc(e.at)}">${fmtDate(e.at)}, ${fmtHM(e.at)}</time><span><b>${esc(e.title)}</b> · ${esc(e.detail)}</span></li>`).join('')}</ol><p class="note">Здесь показаны смены зоны риска. Остальные прогнозы — на графике ниже.</p>`;
}

function rulerHtml(p) {
  const ti = parseTs(p.issued_at);
  const tp = parseTs(p.target_planned);
  if (!Number.isFinite(ti) || !Number.isFinite(tp)) return '';
  const d = isNum(p.delay_s) ? +p.delay_s : 0;
  const lo = isNum(p.delay_q10_s) ? +p.delay_q10_s : d;
  const hi = isNum(p.delay_q90_s) ? +p.delay_q90_s : d;
  const te = tp + d * 1000;
  const a = Math.min(ti, tp + lo * 1000, te);
  const b = Math.max(tp + hi * 1000, te, tp);
  const span = b - a || 60000;
  const A = a - span * 0.04;
  const B = b + span * 0.06;
  const pos = (t) => (((t - A) / (B - A)) * 100).toFixed(2);
  const lead = isNum(p.lead_min) ? +p.lead_min : (tp - ti) / 60000;
  const inWindow = lead >= 10 && lead <= 15;
  const risk = riskOf(p.risk);
  const pe = +pos(te);
  const expAnchor = pe > 80 ? 'end' : pe < 20 ? 'start' : 'mid';
  const pp = +pos(tp);
  const planAnchor = pp > 85 ? 'end' : 'mid';
  return `
    <div class="ruler ruler--${risk}" role="img" aria-label="Прогноз выдан в ${fmtClock(p.issued_at)}, за ${fmtNum(lead, 1)} мин до планового прибытия в ${fmtClock(p.target_planned)}; ожидаемое прибытие ${fmtClock(te)}">
      <span class="ruler__lead" style="left:${pos(ti)}%;width:${(pos(tp) - pos(ti)).toFixed(2)}%"><span>за ${fmtNum(lead, lead % 1 ? 1 : 0)} мин до события</span></span>
      <span class="ruler__axis"></span>
      <span class="ruler__band" style="left:${pos(tp + lo * 1000)}%;width:${(pos(tp + hi * 1000) - pos(tp + lo * 1000)).toFixed(2)}%" title="Интервал 10–90 %"></span>
      <i class="ruler__pt ruler__pt--issue" style="left:${pos(ti)}%"></i>
      <i class="ruler__pt ruler__pt--plan" style="left:${pos(tp)}%"></i>
      <i class="ruler__pt ruler__pt--exp" style="left:${pos(te)}%"></i>
      <span class="ruler__lbl ruler__lbl--exp is-${expAnchor}" style="left:${pos(te)}%">ожидается <b>${fmtClock(te)}</b></span>
      <span class="ruler__lbl ruler__lbl--issue" style="left:${pos(ti)}%">прогноз <b>${fmtClock(p.issued_at)}</b></span>
      <span class="ruler__lbl ruler__lbl--plan is-${planAnchor}" style="left:${pos(tp)}%">план <b>${fmtClock(p.target_planned)}</b></span>
    </div>
    <p class="ruler__note ${inWindow ? 'is-ok' : 'is-off'}">${inWindow ? 'Упреждение в целевом окне 10–15 мин.' : 'Упреждение вне окна 10–15 мин: у ближайших остановок нет планового времени в этом окне.'}</p>`;
}

function contribHtml(p) {
  const list = (p?.contributions || []).filter((c) => isNum(c.shap));
  if (!list.length) return '<p class="empty">Для этого прогноза вклад признаков не рассчитан (резервная эвристика или старая модель).</p>';
  const top = Math.max(...list.map((c) => Math.abs(+c.shap))) || 1;
  return (
    `<ul class="contrib">${list
      .map((c) => {
        const v = +c.shap;
        const w = ((Math.abs(v) / top) * 50).toFixed(1);
        return `<li><span class="contrib__t">${esc(c.text || c.feature)}</span><span class="contrib__bar" aria-hidden="true"><i class="${v >= 0 ? 'is-pos' : 'is-neg'}" style="width:${w}%"></i></span><b class="contrib__v">${v >= 0 ? '+' : '−'}${fmtNum(Math.abs(v))} с</b></li>`;
      })
      .join('')}</ul>` + '<p class="note">Вклад в прогноз задержки по SHAP: справа — в сторону опоздания, слева — в сторону опережения.</p>'
  );
}

function upcomingHtml(detail, p, trip) {
  const up = detail?.upcoming || [];
  if (!up.length) return `<p class="empty">${detail ? 'Нет данных о ближайших остановках.' : 'Загружаем…'}</p>`;
  const head = trip ? '<th scope="col">Остановка</th><th scope="col" class="num">План</th><th scope="col" class="num">При текущем отклонении</th>' : '<th scope="col">Остановка</th><th scope="col" class="num">План</th>';
  const rows = up
    .map((u) => {
      const isT = p && String(u.stop_id) === String(p.target_stop_id);
      const cells = trip ? `<td class="num">${fmtClock(u.planned)}</td><td class="num">${isT ? `<b class="t-${riskOf(p.risk)}">прогноз ${fmtDelay(p.delay_s)}</b>` : u.eta ? fmtClock(u.eta) : '—'}</td>` : `<td class="num">${fmtHM(u.planned)}</td>`;
      return `<tr${isT ? ' class="is-target"' : ''}><td>${esc(u.name || u.stop_id)}${isT ? ' <span class="tag">цель</span>' : ''}</td>${cells}</tr>`;
    })
    .join('');
  return `<table class="tbl tbl--compact"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}

export function renderReview(s) {
  const body = $('rv-body');
  const v = vehicleById(s, s.selected);
  if (!v) {
    body._key = null;
    setHTML(body, '<p class="empty">ТС не выбрано.</p>');
    return;
  }
  const key = `v:${v.tr_id}`;
  if (body._key !== key) {
    body._key = key;
    body._html = null;
    body.innerHTML = `
      <p class="dlg__lead" id="rv-lead"></p>
      <section class="rv" id="rv-inc-s" hidden><h3>История инцидента</h3><div id="rv-inc"></div></section>
      <section class="rv" id="rv-ruler-s"><h3>Когда выдан прогноз</h3><div id="rv-ruler"></div></section>
      <section class="rv"><h3>Отклонение от графика: факт и прогнозы</h3>
        <p class="legend-row"><span><i class="k k--act"></i>факт на остановках</span><span><i class="k k--est"></i>оценка между остановками</span><span><i class="k k--prd"></i>прогнозы</span><span><i class="k k--cur"></i>текущий прогноз и интервал</span></p>
        <p class="rv__hint">Точка прогноза стоит в момент события, к которому он выдан, — на 10–15 мин правее момента выдачи. Наведите на неё: линия покажет, когда прогноз выдан и какое было отклонение в тот момент, крестик — факт на этой остановке.</p>
        <div class="chart" id="rv-chart"></div></section>
      <div class="rv-grid">
        <section class="rv"><h3>Что сильнее всего повлияло на прогноз</h3><div id="rv-contrib"></div></section>
        <section class="rv"><h3 id="rv-up-t">Остановки впереди</h3><div id="rv-up"></div></section>
      </div>`;
  }
  const it = incidents(s).find((x) => x.key === String(v.tr_id));
  const alert = it?.alert && !it.alert.synthetic ? it.alert : null;
  const p = effectivePrediction(v, it?.alert);
  const detail = s.detail && String(s.detail.tr_id) === String(v.tr_id) ? s.detail : null;
  const trip = inTrip(v);
  setHTML($('rv-lead'), `ТС ${esc(v.tr_id)}, ${esc(v.route_name || 'маршрут не указан')}.${p ? ` Прогноз ${fmtDelay(p.delay_s)} к «${esc(p.target_stop_name || '—')}».` : ' Текущего прогноза нет.'}`);
  $('rv-inc-s').hidden = !alert;
  setHTML($('rv-inc'), alert ? incidentHistoryHtml(alert) : '');
  $('rv-ruler-s').hidden = !p;
  setHTML($('rv-ruler'), p ? rulerHtml(p) : '');
  setHTML($('rv-contrib'), p ? contribHtml(p) : '<p class="empty">Текущего прогноза нет.</p>');
  setHTML($('rv-up-t'), trip ? 'Остановки впереди' : 'Остановки следующего рейса');
  setHTML($('rv-up'), s.detailErr && !detail ? `<p class="err">${esc(s.detailErr)}</p>` : upcomingHtml(detail, p, trip));
  const box = $('rv-chart');
  if (detail) {
    const sig = JSON.stringify([detail.delay_history?.length, detail.delay_history?.[detail.delay_history.length - 1], p?.issued_at, p?.delay_s, streamTime(s)?.slice(0, 16), box.clientWidth]);
    if (box._sig !== sig) {
      box._sig = sig;
      renderDelayChart(box, { history: detail.delay_history, prediction: p, now: streamTime(s) });
    }
  } else if (!box._sig) {
    setHTML(box, s.detailErr ? `<p class="err">${esc(s.detailErr)}</p>` : '<p class="empty">Загружаем историю…</p>');
  }
}
