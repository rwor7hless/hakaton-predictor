import { riskOf, fmtHM, fmtDelay, fmtPct, esc } from './format.js';

// Короткая формулировка причины открытия для истории инцидента в «Разборе».
export function reasonText(r) {
  if (!r) return '';
  switch (r.rule) {
    case 'delay_late':
      return `Опоздание ${fmtDelay(r.value)}`;
    case 'delay_early':
      return `Опережение ${fmtDelay(r.value)}`;
    case 'p_late':
      return `Вероятность опоздания ${fmtPct(r.value)}`;
    case 'p_early':
      return `Вероятность опережения ${fmtPct(r.value)}`;
    default:
      return 'Риск отклонения от графика';
  }
}

export const CLOSED = { calm: 'прогноз вернулся в норму', trip_end: 'рейс завершён', no_data: 'от ТС перестали приходить данные', no_forecast: 'прогноз больше не строится' };

// Что делать: главная рекомендация и отметка «в работе» — в начале карточки.
export function todoHtml(s, p, it) {
  const a = it?.alert;
  if (!a || a.synthetic || it.cleared) return '';
  const recs = p?.recommendations || [];
  const busy = s.ackBusy === String(a.id);
  const btn = a.ack_at
    ? `<p class="todo__ack"><i aria-hidden="true"></i>В работе с ${fmtHM(a.ack_at)}</p><button type="button" class="btn btn--sm" data-act="unack" data-id="${esc(a.id)}"${busy ? ' disabled' : ''}>Снять «в работе»</button>`
    : `<button type="button" class="btn btn--primary" data-act="ack" data-id="${esc(a.id)}"${busy ? ' disabled' : ''}>Взять в работу</button>`;
  return (
    `<div class="todo todo--${riskOf(a.risk)}">` +
    (recs.length ? `<p class="todo__t">${esc(recs[0])}</p>` : '') +
    `<div class="todo__act">${btn}</div>` +
    (s.ackErr ? `<p class="err">${esc(s.ackErr)}</p>` : '') +
    `</div>`
  );
}
