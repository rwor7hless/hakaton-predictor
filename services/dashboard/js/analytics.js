import { RISK_LABEL, riskOf, fmtHM, fmtDelay, fmtAbsDur, fmtNum, fmtPct, fmtMin, esc, isNum } from './format.js';
import { hotspotKey } from './state.js';
import { $, setHTML, ICON, plural } from './dom.js';

// Каркас панели создаётся один раз: заголовок и прокручиваемое тело обновляются отдельно,
// поэтому прокрутка таблиц не сбрасывается при опросе.
function fill(id, head, body, scroll = false) {
  const el = $(id);
  if (!el._frame) {
    el.innerHTML = `<div class="an__head"></div><div class="${scroll ? 'an__scroll' : 'an__body'}"></div>`;
    el._frame = true;
  }
  setHTML(el.firstChild, head);
  setHTML(el.lastChild, body);
}

const sec = (x, d = 0) => (isNum(x) ? `${fmtNum(x, d)} с` : '—');
const ms = (x) => (isNum(x) ? `${fmtNum(x, +x < 10 ? 1 : 0)} мс` : '—');

// Горизонтальные столбики MAE на общей шкале: модель выделена, эталоны серые.
function maeBars(groups) {
  const vals = groups.flatMap((g) => g.rows.map((r) => r.v)).filter(isNum);
  if (!vals.length) return '<p class="empty">Метрики ещё не получены.</p>';
  const top = Math.max(...vals) * 1.08;
  return groups
    .map(
      (g) =>
        `<figure class="bars"><figcaption>${g.title}</figcaption><ul>${g.rows
          .map((r) => {
            const w = isNum(r.v) ? Math.max(1.5, (r.v / top) * 100) : 0;
            return `<li class="bars__row${r.main ? ' is-main' : ''}" title="${esc(`${r.label}: ${sec(r.v, 1)}${r.hint ? `. ${r.hint}` : ''}`)}"><span class="bars__l">${r.label}</span><span class="bars__track"><i style="width:${w.toFixed(1)}%"></i></span><b class="bars__v">${sec(r.v, r.v < 100 ? 1 : 0)}</b></li>`;
          })
          .join('')}</ul></figure>`,
    )
    .join('');
}

function renderQuality(s) {
  const m = s.metrics;
  const on = m?.online || {};
  const off = m?.offline || {};
  const sum = s.snap?.summary || {};
  const mae = on.mae_s ?? sum.online_mae_s;
  const n = on.n ?? sum.online_n;
  const lead = on.mean_lead_min ?? sum.mean_lead_min;
  const err = !m && s.metricsErr ? `<p class="err">Метрики недоступны: ${esc(s.metricsErr)}</p>` : '';
  const bars = maeBars([
    {
      title: `На потоке${isNum(n) ? `, ${fmtNum(n)} ${plural(+n, 'наступившее событие', 'наступивших события', 'наступивших событий')}` : ''}`,
      rows: [
        { label: 'Модель', v: mae, main: true, hint: 'Средняя абсолютная ошибка по уже наступившим событиям' },
        { label: 'Без модели', v: on.naive_mae_s, hint: 'Прогноз «текущее отклонение сохранится»' },
      ],
    },
    {
      title: 'На отложенной выборке',
      rows: [
        { label: 'Модель', v: off.test_mae_s, main: true, hint: 'Ансамбль CatBoost + GRU на labels_test' },
        { label: 'Текущее отклонение', v: off.baseline_mae_s, hint: 'Baseline организаторов: cur_dev_s' },
        { label: 'Нулевой прогноз', v: off.zero_mae_s, hint: 'Отклонение всегда 0' },
      ],
    },
  ]);
  const cov = off.interval_coverage;
  fill(
    'an-quality',
    `<header class="panel__head"><div><h2 class="panel__title" id="an-q-t">Качество прогноза</h2><p class="panel__sub">Средняя абсолютная ошибка, меньше — лучше</p></div></header>`,
    `${err}${bars}<dl class="kv kv--grid">` +
      `<div><dt>Прогнозов за 10–15 мин до события</dt><dd>${fmtPct(on.horizon_ok_share)}</dd></div>` +
      `<div><dt>Среднее упреждение</dt><dd>${fmtMin(lead, 1)}</dd></div>` +
      `<div><dt>Факт внутри интервала 10–90 %</dt><dd>${fmtPct(cov, 1)}${isNum(cov) ? '<small> при цели 80 %</small>' : ''}</dd></div>` +
      `<div${+on.retro_predictions > 0 ? ' class="is-warn"' : ''}><dt>Прогнозов задним числом</dt><dd>${isNum(on.retro_predictions) ? fmtNum(on.retro_predictions) : '—'}</dd></div>` +
      `</dl>`,
  );
}

function renderPerf(s) {
  const m = s.metrics;
  const h = s.snap?.health;
  const lat = m?.latency || {};
  const thr = m?.throughput || {};
  const st = h?.stream || {};
  const ml = h?.ml?.state;
  fill(
    'an-perf',
    `<header class="panel__head"><div><h2 class="panel__title" id="an-p-t">Производительность</h2><p class="panel__sub">Медиана и 95-й перцентиль за последние прогнозы</p></div></header>`,
    `<dl class="kv kv--rows">` +
      `<div><dt>От пакета до прогноза</dt><dd>${ms(lat.ingest_to_prediction_ms_p50)}<small> p95 ${ms(lat.ingest_to_prediction_ms_p95)}</small></dd></div>` +
      `<div><dt>Расчёт модели</dt><dd>${ms(lat.ml_predict_ms_p50)}<small> p95 ${ms(lat.ml_predict_ms_p95)}</small></dd></div>` +
      `<div><dt>Возраст данных в момент прогноза</dt><dd>${ms(lat.data_age_at_prediction_ms_p50)}</dd></div>` +
      `<div><dt>Пакетов в секунду</dt><dd>${isNum(thr.packets_per_s ?? st.packets_per_s) ? fmtNum(thr.packets_per_s ?? st.packets_per_s, 1) : '—'}<small> очередь ${isNum(thr.queue_len) ? fmtNum(thr.queue_len) : '—'}</small></dd></div>` +
      `<div><dt>Бортовых блоков на связи</dt><dd>${isNum(st.connected_units) ? `${fmtNum(st.connected_units)} из ${fmtNum(st.known_units)}` : '—'}</dd></div>` +
      `<div${+st.crc_errors > 0 ? ' class="is-warn"' : ''}><dt>Пакетов с ошибкой CRC</dt><dd>${isNum(st.crc_errors) ? fmtNum(st.crc_errors) : '—'}</dd></div>` +
      `<div><dt>Прогнозов выдано</dt><dd>${isNum(thr.predictions_total) ? fmtNum(thr.predictions_total) : '—'}</dd></div>` +
      `<div${ml && ml !== 'ok' ? ' class="is-warn"' : ''}><dt>ML-модель</dt><dd>${ml ? (ml === 'ok' ? 'работает' : 'резервная эвристика') : '—'}${h?.ml?.model_version ? `<small> версия ${esc(h.ml.model_version)}</small>` : ''}</dd></div>` +
      `</dl>`,
  );
}

function renderHot(s) {
  const all = Array.isArray(s.hotspots) ? s.hotspots : [];
  const rows = all
    .map((h) => {
      const key = hotspotKey(h);
      const share = Math.max(0, Math.min(1, +h.risk_share || 0));
      const tone = share >= 0.6 ? 'red' : share >= 0.3 ? 'yellow' : 'green';
      const causes = (h.top_causes || []).slice(0, 3).map((c) => c.text || c.code).filter(Boolean);
      return (
        `<tr${s.hotspot === key ? ' class="is-sel"' : ''}>` +
        `<td class="num"><span class="share share--${tone}" title="${isNum(h.n) ? `По ${fmtNum(h.n)} ${plural(+h.n, 'прогнозу', 'прогнозам', 'прогнозам')}` : ''}"><i style="width:${(share * 100).toFixed(0)}%"></i></span><b>${fmtPct(share)}</b></td>` +
        `<td><span class="cell-seg">${esc(h.from_stop || '—')} ${ICON.arrow} ${esc(h.to_stop || '—')}</span><span class="cell-sub">${esc(h.route_name || '')}</span></td>` +
        `<td class="num">${esc(h.tr_id)}</td>` +
        `<td class="num">${fmtDelay(h.mean_delay_s)}</td>` +
        `<td class="num">${fmtDelay(h.max_delay_s)}</td>` +
        `<td class="cell-causes">${esc(causes.join('; ')) || '—'}</td>` +
        `<td><button type="button" class="lnk" data-act="hotspot" data-key="${esc(key)}">На карте</button></td>` +
        `</tr>`
      );
    })
    .join('');
  const empty = all.length ? '' : `<p class="empty">${s.hotspotsErr ? `Список участков недоступен: ${esc(s.hotspotsErr)}` : 'Проблемных участков пока нет: статистика копится по мере прогнозов.'}</p>`;
  fill(
    'an-hot',
    `<header class="panel__head"><div><h2 class="panel__title" id="an-h-t">Проблемные участки</h2><p class="panel__sub">Где за последний час прогноз чаще всего уходил в риск</p></div>${all.length ? `<span class="count">${fmtNum(all.length)}</span>` : ''}</header>`,
    all.length
      ? `<table class="tbl"><thead><tr><th scope="col" class="num">В риске</th><th scope="col">Участок</th><th scope="col" class="num">ТС</th><th scope="col" class="num">Среднее</th><th scope="col" class="num">Макс.</th><th scope="col">Частые причины</th><th scope="col"><span class="sr">Действие</span></th></tr></thead><tbody>${rows}</tbody></table>`
      : empty,
    true,
  );
}

function renderLog(s) {
  const list = [...(s.resolved || [])].sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  const errOf = (a) => (isNum(a.error_s) ? +a.error_s : isNum(a.actual_delay_s) && isNum(a.prediction?.delay_s) ? a.actual_delay_s - a.prediction.delay_s : null);
  const errs = list.map(errOf).filter(isNum);
  const mean = errs.length ? errs.reduce((x, y) => x + Math.abs(y), 0) / errs.length : null;
  const acked = list.filter((a) => a.ack_at).length;
  const rows = list
    .map((a) => {
      const p = a.prediction || {};
      const e = errOf(a);
      const r = riskOf(a.risk);
      return (
        `<tr>` +
        `<td class="num">${fmtHM(a.updated_at)}</td>` +
        `<td><button type="button" class="lnk" data-act="goto" data-id="${esc(a.tr_id)}" title="Открыть ТС на мониторинге">${esc(a.tr_id)}</button></td>` +
        `<td><i class="dot dot--${r}" title="${RISK_LABEL[r]}"></i></td>` +
        `<td class="cell-stop">${esc(p.target_stop_name || '—')}</td>` +
        `<td class="num">${fmtHM(p.target_planned)}</td>` +
        `<td class="num">${fmtDelay(p.delay_s)}</td>` +
        `<td class="num">${fmtDelay(a.actual_delay_s)}</td>` +
        `<td class="num"><b>${e != null ? fmtAbsDur(e) : '—'}</b></td>` +
        `<td class="num">${a.ack_at ? fmtHM(a.ack_at) : '—'}</td>` +
        `</tr>`
      );
    })
    .join('');
  const sub = list.length
    ? `${fmtNum(list.length)} ${plural(list.length, 'инцидент', 'инцидента', 'инцидентов')}${mean != null ? `, средняя ошибка ${fmtAbsDur(mean)}` : ''}${acked ? `, в работе было ${fmtNum(acked)}` : ''}`
    : 'Прогноз и факт по инцидентам, которые уже закрылись';
  fill(
    'an-log',
    `<header class="panel__head"><div><h2 class="panel__title" id="an-l-t">Закрытые инциденты</h2><p class="panel__sub">${sub}</p></div></header>`,
    list.length
      ? `<table class="tbl"><thead><tr><th scope="col" class="num">Закрыт</th><th scope="col">ТС</th><th scope="col"><span class="sr">Риск</span></th><th scope="col">Остановка</th><th scope="col" class="num">План</th><th scope="col" class="num">Прогноз</th><th scope="col" class="num">Факт</th><th scope="col" class="num">Ошибка</th><th scope="col" class="num">В работе с</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="empty">${s.resolvedErr ? `Журнал недоступен: ${esc(s.resolvedErr)}` : 'Закрытых инцидентов пока нет. Они появятся, когда ТС пройдут целевые остановки.'}</p>`,
    true,
  );
}

export function renderAnalytics(s) {
  if (s.view !== 'analytics') return;
  renderQuality(s);
  renderPerf(s);
  renderHot(s);
  renderLog(s);
}
