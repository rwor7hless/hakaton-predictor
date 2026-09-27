import { RISK_LABEL, esc } from './format.js';

export const $ = (id) => document.getElementById(id);

export function setHTML(el, html) {
  if (el && el._html !== html) {
    el.innerHTML = html;
    el._html = html;
  }
}

// Список кнопок с сохранением DOM-узлов между кадрами: фокус и прокрутка не сбрасываются.
// Строки, появившиеся после первой отрисовки, получают на пару секунд класс is-new (подсветка в CSS).
export function patchButtons(ul, items) {
  const existing = new Map([...ul.children].map((li) => [li.dataset.key, li]));
  const seen = ul._seen;
  let prev = null;
  for (const it of items) {
    let li = existing.get(it.key);
    if (!li) {
      li = document.createElement('li');
      li.dataset.key = it.key;
      if (seen) {
        li.classList.add('is-new');
        setTimeout(() => li.classList.remove('is-new'), 2000);
      }
      const b = document.createElement('button');
      b.type = 'button';
      li.appendChild(b);
    }
    existing.delete(it.key);
    const b = li.firstChild;
    if (b.className !== it.cls) b.className = it.cls;
    for (const [k, v] of Object.entries(it.attrs || {})) {
      if (v == null) b.removeAttribute(k);
      else if (b.getAttribute(k) !== String(v)) b.setAttribute(k, v);
    }
    if (b._html !== it.html) {
      b.innerHTML = it.html;
      b._html = it.html;
    }
    const next = prev ? prev.nextSibling : ul.firstChild;
    if (next !== li) ul.insertBefore(li, next);
    prev = li;
  }
  for (const li of existing.values()) li.remove();
  if (items.length) ul._seen = true;
}

export const ICON = {
  arrow: '<svg class="ic-arrow" viewBox="0 0 24 12" aria-hidden="true"><path d="M1 6h20M16 1l5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  close: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  foldL: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8.5 4 4.5 8l4 4M12.5 4l-4 4 4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  foldR: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7.5 4l4 4-4 4M3.5 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warn: '<svg viewBox="0 0 16 16" aria-hidden="true"><path class="ic-bg" d="M8 1.5 15 14H1z"/><path class="ic-fg" d="M8 6v4" stroke-width="1.6" stroke-linecap="round"/><circle class="ic-dot" cx="8" cy="12" r=".9"/></svg>',
};

export const chip = (risk, text) => `<span class="chip chip--${risk}">${esc(text || RISK_LABEL[risk])}</span>`;

export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  if (b >= 2 && b <= 4) return few;
  return many;
}

// Состояния ТС вне рейса и без расписания: подписи для списков и карточки.
export const GROUP_TITLE = { ...RISK_LABEL, layover: 'На отстое', out_of_service: 'Вне смены', offline: 'Нет связи', no_schedule: 'Без расписания' };
