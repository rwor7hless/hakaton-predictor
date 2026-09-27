import { parseTs, fmtDelay, fmtHM, fmtClock, fmtNum, esc, isNum } from './format.js';

const STEPS = [30, 60, 120, 180, 300, 600, 900, 1800, 3600];
const H = 200;
const M = { l: 46, r: 14, t: 14, b: 26 };

function niceStep(raw) {
  return STEPS.find((s) => s >= raw) || STEPS[STEPS.length - 1];
}

function nearest(list, t, maxMs) {
  let best = null;
  let bd = Infinity;
  for (const p of list) {
    const d = Math.abs(p.t - t);
    if (d < bd) {
      bd = d;
      best = p;
    }
  }
  return bd <= maxMs ? best : null;
}

// Прогноз ставится в момент события, к которому выдан (плановое время цели), — на 10–15 мин
// правее момента выдачи. При наведении линия ведёт от точки прогноза к моменту выдачи.
export function renderDelayChart(el, { history, prediction, now }) {
  const act = []; // факт на остановках
  const est = []; // оценка трекера между остановками
  const prd = [];
  for (const h of history || []) {
    const t = parseTs(h.t);
    if (!Number.isFinite(t) || !isNum(h.delay_s)) continue;
    const v = +h.delay_s;
    if (h.kind === 'predicted') {
      prd.push({ t, v, issued: parseTs(h.issued_at), lead: isNum(h.lead_min) ? +h.lead_min : null, curAt: isNum(h.current_at_issue_s) ? +h.current_at_issue_s : null, fact: isNum(h.actual_s) ? +h.actual_s : null });
    } else (h.kind === 'estimate' ? est : act).push({ t, v });
  }
  act.sort((a, b) => a.t - b.t);
  est.sort((a, b) => a.t - b.t);
  prd.sort((a, b) => a.t - b.t);
  const nowMs = parseTs(now);
  const pt = prediction ? parseTs(prediction.target_planned) : NaN;
  const cur = Number.isFinite(pt) && isNum(prediction.delay_s)
    ? { t: pt, v: +prediction.delay_s, lo: isNum(prediction.delay_q10_s) ? +prediction.delay_q10_s : NaN, hi: isNum(prediction.delay_q90_s) ? +prediction.delay_q90_s : NaN }
    : null;

  if (!act.length && !est.length && !prd.length && !cur) {
    el.innerHTML = '<p class="empty">История задержки для этого ТС пока не накоплена.</p>';
    el._chart = null;
    return;
  }

  const W = Math.max(260, Math.round(el.clientWidth || 360));
  const times = [...act, ...est, ...prd].map((p) => p.t);
  if (cur) times.push(cur.t);
  if (Number.isFinite(nowMs)) times.push(nowMs);
  let x0 = Math.min(...times);
  let x1 = Math.max(...times);
  if (Number.isFinite(nowMs)) x0 = Math.max(x0, nowMs - 60 * 60000);
  if (x1 - x0 < 20 * 60000) x0 = x1 - 20 * 60000;
  x1 += 60000;
  const visAct = act.filter((p) => p.t >= x0);
  const visEst = est.filter((p) => p.t >= x0);
  const visPrd = prd.filter((p) => p.t >= x0);
  const vals = [...visAct, ...visEst, ...visPrd].map((p) => p.v).concat(visPrd.map((p) => p.fact).filter(isNum), [-60, 120]);
  if (cur) vals.push(Number.isFinite(cur.lo) ? cur.lo : cur.v, Number.isFinite(cur.hi) ? cur.hi : cur.v);
  let y0 = Math.min(...vals);
  let y1 = Math.max(...vals);
  const step = niceStep((y1 - y0) / 4);
  y0 = Math.floor(y0 / step) * step;
  y1 = Math.ceil(y1 / step) * step;
  if (y1 === y0) y1 = y0 + step;

  const iw = W - M.l - M.r;
  const ih = H - M.t - M.b;
  const X = (t) => M.l + ((t - x0) / (x1 - x0)) * iw;
  const Y = (v) => M.t + ((y1 - v) / (y1 - y0)) * ih;
  const parts = [];

  for (let v = y0; v <= y1 + 1e-6; v += step) {
    const y = Y(v).toFixed(1);
    parts.push(`<line class="g${v === 0 ? ' g--zero' : ''}" x1="${M.l}" x2="${W - M.r}" y1="${y}" y2="${y}"/>`);
    parts.push(`<text class="ax" x="${M.l - 6}" y="${y}" dy="0.32em" text-anchor="end">${v === 0 ? '0' : fmtDelay(v)}</text>`);
  }
  const tickMs = (x1 - x0) / 60000 > 50 ? 15 * 60000 : 10 * 60000;
  for (let t = Math.ceil(x0 / tickMs) * tickMs; t <= x1; t += tickMs) {
    parts.push(`<text class="ax" x="${X(t).toFixed(1)}" y="${H - 8}" text-anchor="middle">${fmtHM(t)}</text>`);
  }
  for (const [v, label] of [
    [120, 'опоздание'],
    [-60, 'опережение'],
  ]) {
    if (v < y0 || v > y1) continue;
    const y = Y(v).toFixed(1);
    parts.push(`<line class="thr" x1="${M.l}" x2="${W - M.r}" y1="${y}" y2="${y}"/>`);
    parts.push(`<text class="thr-l" x="${M.l + 4}" y="${y}" dy="${v > 0 ? '-0.4em' : '1.1em'}">${label}</text>`);
  }
  if (Number.isFinite(nowMs) && nowMs >= x0 && nowMs <= x1) {
    const x = X(nowMs).toFixed(1);
    parts.push(`<line class="now" x1="${x}" x2="${x}" y1="${M.t}" y2="${H - M.b}"/>`);
    parts.push(`<text class="now-l" x="${x}" y="${M.t - 3}" text-anchor="middle">сейчас</text>`);
  }
  const poly = (list) => list.map((p) => `${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join(' ');
  if (visEst.length > 1) parts.push(`<polyline class="est" points="${poly(visEst)}"/>`);
  if (visAct.length > 1) parts.push(`<polyline class="act" points="${poly(visAct)}"/>`);
  parts.push('<g class="lead" visibility="hidden"></g>');
  for (const p of visPrd) parts.push(`<circle class="prd" cx="${X(p.t).toFixed(1)}" cy="${Y(p.v).toFixed(1)}" r="3.5"/>`);
  if (cur) {
    const x = X(cur.t).toFixed(1);
    if (Number.isFinite(cur.lo) && Number.isFinite(cur.hi)) {
      const a = Y(cur.hi);
      const b = Y(cur.lo);
      parts.push(`<rect class="band" x="${(+x - 7).toFixed(1)}" y="${a.toFixed(1)}" width="14" height="${Math.max(1, b - a).toFixed(1)}" rx="3"/>`);
      parts.push(`<line class="wh" x1="${x}" x2="${x}" y1="${a.toFixed(1)}" y2="${b.toFixed(1)}"/>`);
    }
    parts.push(`<circle class="cur" cx="${x}" cy="${Y(cur.v).toFixed(1)}" r="6"/>`);
  }
  parts.push(`<line class="xh" x1="0" x2="0" y1="${M.t}" y2="${H - M.b}" visibility="hidden"/>`);
  parts.push(`<rect class="hit" x="${M.l}" y="${M.t}" width="${iw}" height="${ih}"/>`);

  const last = visAct[visAct.length - 1];
  const label = `График задержки: факт сейчас ${last ? fmtDelay(last.v) : 'нет данных'}${cur ? `, прогноз ${fmtDelay(cur.v)} на ${fmtHM(cur.t)}` : ''}`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(label)}">${parts.join('')}</svg><div class="ctip" hidden></div>`;
  // Прогнозы к одной остановке (одинаковое плановое время) — одна группа для подсказки.
  const groups = [];
  for (const p of visPrd) {
    const g = groups[groups.length - 1];
    if (g && g.t === p.t) g.items.push(p);
    else groups.push({ t: p.t, items: [p] });
  }
  if (cur) groups.push({ t: cur.t, items: [], cur });
  el._chart = { X, Y, x0, x1, iw, act: visAct, est: visEst, groups, now: nowMs, W };

  if (!el._bound) {
    el._bound = true;
    el.addEventListener('pointermove', (e) => {
      el._hx = e.clientX - el.getBoundingClientRect().left;
      hover(el);
    });
    el.addEventListener('pointerleave', () => {
      el._hx = null;
      hover(el);
    });
  }
  hover(el);
}

function hover(el) {
  const c = el._chart;
  const svg = el.querySelector('svg');
  const tip = el.querySelector('.ctip');
  if (!c || !svg || !tip) return;
  const xh = svg.querySelector('.xh');
  const scale = c.W / (svg.getBoundingClientRect().width || c.W);
  const x = el._hx == null ? null : el._hx * scale;
  if (x == null || x < M.l || x > c.W - M.r) {
    xh.setAttribute('visibility', 'hidden');
    svg.querySelector('.lead')?.setAttribute('visibility', 'hidden');
    tip.hidden = true;
    return;
  }
  const t = c.x0 + ((x - M.l) / c.iw) * (c.x1 - c.x0);
  const a = nearest(c.act, t, 90000);
  const e = nearest(c.est, t, 90000);
  const g = nearest(c.groups, t, 90000);
  xh.setAttribute('x1', x);
  xh.setAttribute('x2', x);
  xh.setAttribute('visibility', 'visible');
  const rows = [`<b>${fmtClock(t).slice(0, 5)}</b>`];
  const lead = svg.querySelector('.lead');
  let leadHtml = '';
  if (g?.cur) {
    rows.push(`<span><i class="k k--cur"></i>текущий прогноз ${fmtDelay(g.cur.v)}${Number.isFinite(g.cur.lo) ? ` (${fmtDelay(g.cur.lo)}…${fmtDelay(g.cur.hi)})` : ''}</span>`);
  } else if (g) {
    const vs = g.items.map((p) => p.v);
    const first = g.items.reduce((m, p) => (Number.isFinite(p.issued) && (!m || p.issued < m.issued) ? p : m), null) || g.items[0];
    const fact = g.items.find((p) => isNum(p.fact))?.fact;
    rows.push(`<span><i class="k k--prd"></i>${vs.length > 1 ? `${vs.length} прогнозов к этой остановке: ${fmtDelay(Math.min(...vs))} … ${fmtDelay(Math.max(...vs))}` : `прогноз к этой остановке ${fmtDelay(vs[0])}`}</span>`);
    if (Number.isFinite(first.issued)) {
      rows.push(`<span>первый выдан в ${fmtHM(first.issued)}${first.lead != null ? `, за ${fmtNum(first.lead, first.lead % 1 ? 1 : 0)} мин` : ''}${first.curAt != null ? `; отклонение тогда ${fmtDelay(first.curAt)}` : ''}</span>`);
    }
    if (isNum(fact)) rows.push(`<span><i class="k k--out"></i>факт на остановке ${fmtDelay(fact)}</span>`);
    else if (Number.isFinite(c.now) && g.t > c.now) rows.push('<span>факт ещё не наступил</span>');
    // Линии от точек прогноза к моменту их выдачи и отклонению в тот момент; крестик — факт.
    for (const p of g.items) {
      if (!Number.isFinite(p.issued) || p.curAt == null || p.issued < c.x0) continue;
      leadHtml += `<line x1="${c.X(p.issued).toFixed(1)}" y1="${c.Y(p.curAt).toFixed(1)}" x2="${c.X(p.t).toFixed(1)}" y2="${c.Y(p.v).toFixed(1)}"/><circle class="lead__from" cx="${c.X(p.issued).toFixed(1)}" cy="${c.Y(p.curAt).toFixed(1)}" r="3"/>`;
    }
    if (isNum(fact)) {
      const fx = c.X(g.t);
      const fy = c.Y(fact);
      leadHtml += `<path class="lead__fact" d="M${(fx - 5).toFixed(1)},${(fy - 5).toFixed(1)}L${(fx + 5).toFixed(1)},${(fy + 5).toFixed(1)}M${(fx + 5).toFixed(1)},${(fy - 5).toFixed(1)}L${(fx - 5).toFixed(1)},${(fy + 5).toFixed(1)}"/>`;
    }
  }
  if (!g?.items?.length) {
    if (a) rows.push(`<span><i class="k k--act"></i>факт на остановке ${fmtDelay(a.v)}</span>`);
    if (e) rows.push(`<span><i class="k k--est"></i>оценка между остановками ${fmtDelay(e.v)}</span>`);
  }
  if (lead) {
    lead.innerHTML = leadHtml;
    lead.setAttribute('visibility', leadHtml ? 'visible' : 'hidden');
  }
  if (rows.length === 1) rows.push('<span>нет точек рядом</span>');
  tip.innerHTML = rows.join('');
  tip.hidden = false;
  const px = x / scale;
  tip.style.left = `${Math.min(Math.max(px + 10, 0), el.clientWidth - tip.offsetWidth - 4)}px`;
  if (px + 10 + tip.offsetWidth > el.clientWidth) tip.style.left = `${Math.max(0, px - tip.offsetWidth - 10)}px`;
}
