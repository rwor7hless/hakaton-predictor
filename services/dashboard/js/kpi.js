import { fmtNum, esc } from './format.js';
import { fleetCounts } from './state.js';
import { $, setHTML } from './dom.js';

// В шапке остаётся только сигнал об отсутствии связи; фильтры живут в левой панели.

// Кнопка-сигнал: число, подпись и переход к первому ТС из списка.
function actKpi(n, label, hint, trId, mod) {
  return (
    `<button type="button" class="kpi kpi--btn kpi--${mod}" data-act="select" data-id="${esc(trId)}" title="${esc(hint)}">` +
    `<b class="kpi__v">${fmtNum(n)}</b><span class="kpi__l">${label}</span></button>`
  );
}

export function renderKpis(s) {
  if (!s.snap) {
    setHTML($('kpis'), '');
    return;
  }
  const c = fleetCounts(s);
  const nd = c.noData;
  const ids = (list) => `${list.slice(0, 5).join(', ')}${list.length > 5 ? ` и ещё ${list.length - 5}` : ''}`;

  setHTML($('kpis'), nd.length
    ? actKpi(nd.length, 'нет связи', `ТС в рейсе, от которых не приходят свежие координаты или бортовой блок не на связи: ${ids(nd.map((v) => v.tr_id))}. Нажмите, чтобы открыть первое`, nd[0].tr_id, 'warn')
    : '');
}
