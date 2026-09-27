import { riskOf, fmtDelay, fmtNum, esc } from './format.js';
import { incidents, vehicleById, effectivePrediction } from './state.js';
import { $, setHTML, ICON } from './dom.js';

// Боковые панели сворачиваются в узкую полосу у края, карта занимает освободившееся место.
// Состояние запоминается в браузере: это удобство одного диспетчера, не данные.

const KEY = 'dash.fold';

export function loadFold() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || '{}');
    return { foldLeft: !!v.left, foldCard: !!v.card };
  } catch {
    return { foldLeft: false, foldCard: false };
  }
}

export function saveFold(s) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ left: s.foldLeft, card: s.foldCard }));
  } catch {
    // Хранилище недоступно (приватный режим): панели просто развернутся при перезагрузке.
  }
}

function leftRail(s) {
  const list = incidents(s).filter((x) => !x.cleared);
  const waiting = list.some((x) => !x.acked);
  return (
    `${ICON.foldR}<b class="fold__n${waiting ? ' is-attn' : ''}">${s.snap ? fmtNum(list.length) : '—'}</b>` +
    `<span class="fold__l">Инциденты</span>`
  );
}

function cardRail(s) {
  const v = s.selected != null ? vehicleById(s, s.selected) : null;
  if (!v) return `${ICON.foldL}<span class="fold__l">Карточка ТС</span>`;
  const it = incidents(s).find((x) => x.key === String(v.tr_id));
  const p = effectivePrediction(v, it?.alert);
  const delay = p ? `<b class="fold__v t-${riskOf(p.risk)}">${fmtDelay(p.delay_s)}</b>` : '';
  return `${ICON.foldL}${delay}<span class="fold__l">ТС ${esc(v.tr_id)}</span>`;
}

export function renderFold(s) {
  const ws = $('ws');
  ws.dataset.left = s.foldLeft ? 'off' : 'on';
  ws.dataset.card = s.foldCard ? 'off' : 'on';
  // Анимации включаются со второго кадра: запомненное состояние при загрузке не «проигрывается».
  if (!ws.classList.contains('is-ready')) requestAnimationFrame(() => requestAnimationFrame(() => ws.classList.add('is-ready')));
  for (const b of document.querySelectorAll('[data-act="fold"]')) {
    const off = b.dataset.side === 'left' ? s.foldLeft : s.foldCard;
    b.setAttribute('aria-expanded', String(!off));
  }
  setHTML($('fold-left'), leftRail(s));
  setHTML($('fold-card'), cardRail(s));
}
