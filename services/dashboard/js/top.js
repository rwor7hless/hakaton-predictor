import { fmtClock, fmtDate, fmtAgo, esc } from './format.js';
import { streamTime } from './state.js';
import { $, setHTML, ICON } from './dom.js';

export function renderClock(s) {
  const t = streamTime(s);
  const el = $('clock');
  el.title = `Время потока телеметрии, ${fmtDate(t)}`;
  setHTML(el, `<b class="clock__time">${fmtClock(t)}</b><span class="clock__meta">${esc(fmtDate(t))}</span>`);
}

const LINK_TEXT = {
  ws: ['WebSocket', 'ok'],
  poll: ['Опрос раз в 2 с', 'warn'],
  down: ['Нет связи', 'bad'],
  connecting: ['Подключение', 'warn'],
  mock: ['Демо-поток', 'ok'],
};

export function renderLink(s) {
  const l = s.link || {};
  const [label, tone] = LINK_TEXT[l.transport] || LINK_TEXT.connecting;
  const age = l.lastAt ? (Date.now() - l.lastAt) / 1000 : null;
  const stale = age != null && age > 6;
  const sub = l.lastAt ? `обновлено ${fmtAgo(age)} назад` : l.error ? esc(l.error) : 'ждём первые данные';
  let html = `<div class="link link--${stale ? 'bad' : tone}" role="status" title="${label}: ${sub}"><i class="link__dot"></i><span class="link__txt"><b>${label}</b><span>${sub}</span></span></div>`;
  if (s.mode === 'mock') {
    const why = s.demoReason === 'unavailable' ? 'Backend недоступен: показаны синтетические данные по расписанию validate' : 'Включён режим моков (?mock=1)';
    html += `<span class="demo" title="${esc(why)}">Демо-данные</span>`;
    if (s.backendBack) html += `<button type="button" class="btn btn--sm" data-act="reload">Backend доступен, переключиться</button>`;
  }
  setHTML($('link'), html);
}

export function renderBanners(s) {
  const out = [];
  const h = s.snap?.health;
  const l = s.link || {};
  const age = l.lastAt ? (Date.now() - l.lastAt) / 1000 : null;
  if (s.mode !== 'mock' && (l.transport === 'down' || (age != null && age > 6))) {
    out.push(
      s.snap
        ? `<div class="banner banner--bad" role="alert">${ICON.warn}<p><b>Нет связи с сервером${age != null ? ` ${fmtAgo(age)}` : ''}.</b> На экране последнее полученное состояние, переподключаемся автоматически.</p></div>`
        : `<div class="banner banner--bad" role="alert">${ICON.warn}<p><b>Сервер не отвечает, данных пока нет.</b> Переподключаемся автоматически. Демо без сервера: откройте страницу с параметром ?mock=1.</p></div>`,
    );
  }
  if (h) {
    const reasons = [];
    const st = h.stream?.state;
    if (st && st !== 'live') {
      reasons.push(st === 'stale' ? `новые пакеты телеметрии не поступают, последний в ${fmtClock(h.stream.last_packet_at)}` : 'телеметрия ещё не поступала');
    }
    if (h.ml?.state && h.ml.state !== 'ok') reasons.push('ML-модель недоступна, прогноз строит резервная эвристика');
    if (h.status === 'degraded' && !reasons.length) reasons.push('сервис сообщает о частичной неработоспособности');
    if (h.status === 'degraded' || (st && st !== 'live')) {
      out.push(`<div class="banner banner--warn" role="alert">${ICON.warn}<p><b>Работа по последнему состоянию.</b> ${esc(reasons.join('; '))}.</p></div>`);
    }
  }
  const box = $('banners');
  setHTML(box, out.join(''));
  box.hidden = !out.length;
}

const FLOW_TEXT = { running: 'идёт', paused: 'на паузе', idle: 'остановлен', finished: 'проигран' };

export function renderFlowButton(s) {
  const r = s.replayer;
  const st = !r && s.replayerErr ? 'error' : r?.state || 'unknown';
  const text = st === 'error' ? 'Проигрыватель недоступен' : r ? `Поток ${FLOW_TEXT[st] || esc(st)}${st === 'running' && r.speed ? `, ×${esc(r.speed)}` : ''}` : 'Поток';
  const btn = $('flow-btn');
  btn.dataset.state = st;
  btn.title = 'Управление проигрыванием исторического потока';
  setHTML(btn, `<i class="flowbtn__dot" aria-hidden="true"></i><span>${text}</span>`);
}
