import { config, createHttpApi } from './api.js';
import { createMockApi } from './mock.js';
import { store, vehicleById, incidents, effectivePrediction, hotspotByKey } from './state.js';
import { createMap } from './map.js';
import { fmtDelay, esc } from './format.js';
import { $, setHTML, ICON } from './dom.js';
import { renderClock, renderLink, renderBanners, renderFlowButton } from './top.js';
import { renderKpis } from './kpi.js';
import { renderLeft } from './left.js';
import { renderCard } from './card.js';
import { renderStrip } from './strip.js';
import { renderFold, loadFold, saveFold } from './fold.js';
import { renderAnalytics } from './analytics.js';
import { renderStream, renderWhatif, renderReview, setWhatifScenario, WHATIF_PARAM } from './dialogs.js';

const errText = (e) => (e && e.message) || String(e);

async function chooseApi() {
  if (config.mock === '1') return { api: await createMockApi({ reason: 'forced' }), reason: 'forced' };
  const http = createHttpApi();
  if (config.mock === '0') return { api: http, reason: null };
  try {
    const h = await http.health();
    if (h && (h.status || h.stream)) return { api: http, reason: null };
    throw new Error('Некорректный ответ /api/health');
  } catch {
    return { api: await createMockApi({ reason: 'unavailable' }), reason: 'unavailable' };
  }
}

function every(ms, fn) {
  let busy = false;
  const run = async () => {
    if (busy) return;
    busy = true;
    try {
      await fn();
    } finally {
      busy = false;
    }
  };
  run();
  return setInterval(run, ms);
}

const DIALOG_RENDER = { 'dlg-stream': renderStream, 'dlg-whatif': renderWhatif, 'dlg-review': renderReview };

function renderOpenDialog(s) {
  const d = s.dialog && $(s.dialog);
  if (d?.open) DIALOG_RENDER[s.dialog]?.(s);
}

async function boot() {
  const { api, reason } = await chooseApi();
  store.set({ api, mode: api.mode, demoReason: reason, ...loadFold() });
  document.body.classList.toggle('is-demo', api.mode === 'mock');

  let mapCtl = null;
  try {
    mapCtl = createMap($('map'), {
      onSelect: (id) => select(id),
      onTiles: (st) => store.set({ tiles: st }),
    });
  } catch (e) {
    $('map').innerHTML = `<p class="empty">Карта не загрузилась: ${esc(errText(e))}</p>`;
  }

  let focusSig = '';
  let flyPending = false;
  let fittedOnce = false;

  function syncMap(s, keys) {
    if (!mapCtl) return;
    if (keys.has('routes')) {
      mapCtl.setRoutes(s.routes);
      if (!fittedOnce && mapCtl.hasRoutes()) {
        fittedOnce = true;
        mapCtl.fitAll();
      }
    }
    if (keys.has('showCtx')) {
      mapCtl.setShowUnscheduled(s.showCtx);
      const b = document.querySelector('[data-act="ctx"]');
      if (b) b.setAttribute('aria-pressed', String(!!s.showCtx));
    }
    if (keys.has('snap') || keys.has('selected') || keys.has('filter')) {
      const filterIds = s.filter === 'unacked'
        ? incidents(s).filter((x) => !x.cleared && !x.acked && !x.alert.synthetic).map((x) => x.key)
        : null;
      mapCtl.setVehicles(s.snap?.vehicles || [], { selected: s.selected, filter: filterIds ? null : s.filter, filterIds, streamTime: s.snap?.stream_time });
    }
    if (keys.has('snap') || keys.has('selected') || keys.has('detail')) {
      const v = vehicleById(s, s.selected);
      const alert = v ? incidents(s).find((x) => x.key === String(v.tr_id))?.alert : null;
      const p = v ? effectivePrediction(v, alert) : null;
      const detail = s.detail && String(s.detail.tr_id) === String(s.selected) ? s.detail : null;
      const seg = p?.segment;
      const sig = JSON.stringify([
        s.selected,
        p?.target_stop_id,
        p?.delay_s,
        p?.risk,
        seg && [seg.from_lat, seg.from_lon, seg.to_lat, seg.to_lon],
        detail?.track?.length,
        detail?.track?.[detail.track.length - 1]?.[2],
        !!detail?.upcoming?.length,
      ]);
      if (sig !== focusSig) {
        focusSig = sig;
        mapCtl.setFocus(v ? { vehicle: v, prediction: p, track: detail?.track, upcoming: detail?.upcoming } : {});
      }
      if (flyPending && v) {
        flyPending = false;
        mapCtl.flyToFocus();
      }
    }
  }

  function renderHotNote(s) {
    const h = hotspotByKey(s, s.hotspot);
    const el = $('map-hot');
    el.hidden = !h;
    setHTML(
      el,
      h
        ? `<span>Участок ${esc(h.from_stop || '—')} ${ICON.arrow} ${esc(h.to_stop || '—')}, ТС ${esc(h.tr_id)}, в среднем ${fmtDelay(h.mean_delay_s)}</span><button type="button" class="icon-btn" data-act="hotspot-clear" aria-label="Убрать участок с карты">${ICON.close}</button>`
        : '',
    );
  }

  function applyView(view) {
    const app = $('app');
    app.dataset.view = view;
    $('view-monitor').hidden = view !== 'monitor';
    $('view-analytics').hidden = view !== 'analytics';
    for (const b of document.querySelectorAll('[data-act="view"]')) {
      const on = b.dataset.view === view;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    if (view === 'monitor' && mapCtl) requestAnimationFrame(() => mapCtl.invalidate());
  }

  // Панель свернули или развернули: карта меняет размер в том же кадре и сдвигается так,
  // чтобы изображение осталось на месте — открывается или закрывается только край.
  function keepMapInPlace(before) {
    const map = mapCtl?.map;
    if (!map || !before) return;
    const after = $('map').getBoundingClientRect();
    if (after.width === before.width && after.left === before.left) return;
    map.resize();
    const shift = after.left - before.left + (after.width - before.width) / 2;
    if (shift) map.panBy([shift, 0], { duration: 0 });
  }

  store.on((s, keys) => {
    const mapBefore = keys.has('foldLeft') || keys.has('foldCard') ? $('map').getBoundingClientRect() : null;
    if (keys.has('tick')) {
      renderLink(s);
      renderBanners(s);
      return;
    }
    if (keys.has('tiles')) {
      const note = $('map-note');
      note.hidden = s.tiles !== 'down';
      note.textContent = 'Подложка карты недоступна: маршруты и ТС показаны без неё.';
      return;
    }
    if (keys.has('view')) {
      applyView(s.view);
      renderAnalytics(s);
    }
    if (['replayer', 'replayerErr', 'playerBusy', 'playerSpeed'].some((k) => keys.has(k))) {
      renderFlowButton(s);
      if (s.dialog === 'dlg-stream') renderStream(s);
    }
    if (keys.has('snap') || keys.has('link') || keys.has('mode') || keys.has('backendBack')) {
      renderClock(s);
      renderLink(s);
      renderBanners(s);
    }
    if (keys.has('snap')) renderKpis(s);
    if (['snap', 'filter', 'selected', 'leftTab', 'showUnsched'].some((k) => keys.has(k))) renderLeft(s);
    if (['snap', 'selected', 'detail', 'detailErr', 'ackBusy', 'ackErr'].some((k) => keys.has(k))) renderCard(s);
    if (['snap', 'selected', 'detail', 'detailErr', 'routes'].some((k) => keys.has(k))) renderStrip(s);
    if (['snap', 'selected', 'foldLeft', 'foldCard'].some((k) => keys.has(k))) renderFold(s);
    if (mapBefore) {
      keepMapInPlace(mapBefore);
      saveFold(s);
    }
    if (['snap', 'selected', 'detail', 'detailErr', 'whatif', 'dialog'].some((k) => keys.has(k))) renderOpenDialog(s);
    if (['snap', 'metrics', 'metricsErr', 'hotspots', 'hotspotsErr', 'hotspot', 'resolved', 'resolvedErr'].some((k) => keys.has(k))) renderAnalytics(s);
    if (keys.has('hotspot') || keys.has('hotspots')) renderHotNote(s);
    syncMap(s, keys);
    if (keys.has('snap') && !s.autoSelected && s.selected == null) autoSelect(s);
  });

  applyView(store.s.view);
  renderClock(store.s);
  renderLink(store.s);
  renderBanners(store.s);
  renderFlowButton(store.s);
  renderKpis(store.s);
  renderLeft(store.s);
  renderCard(store.s);
  renderStrip(store.s);
  renderFold(store.s);

  // Геометрия маршрутов статична: запрашиваем, пока не получим, затем раз в 5 минут.
  // Риск по участкам карта берёт из прогнозов ТС, отдельный опрос не нужен.
  let geomAt = 0;
  const refreshRoutes = async () => {
    if (store.s.routes.length && Date.now() - geomAt < 300000) return;
    try {
      const routes = await api.routes(true);
      if (Array.isArray(routes) && routes.some((r) => Array.isArray(r.path))) {
        geomAt = Date.now();
        store.set({ routes });
      }
    } catch {}
  };
  every(3000, refreshRoutes);

  every(5000, async () => {
    try {
      const list = await api.hotspots(15);
      store.set({ hotspots: Array.isArray(list) ? list : [], hotspotsErr: null });
    } catch (e) {
      store.set({ hotspotsErr: errText(e) });
    }
  });

  every(5000, async () => {
    try {
      store.set({ metrics: await api.metrics(), metricsErr: null });
    } catch (e) {
      store.set({ metricsErr: errText(e) });
    }
  });

  const refreshPlayer = async () => {
    try {
      const r = await api.replayer.status();
      // Скорость идущего потока показываем как выбранную; у остановленного выбор остаётся за пользователем.
      const live = r?.state === 'running' || r?.state === 'paused';
      store.set({ replayer: r, replayerErr: null, ...(live && r.speed ? { playerSpeed: +r.speed } : {}) });
    } catch (e) {
      store.set({ replayer: null, replayerErr: errText(e) });
    }
  };
  every(2000, refreshPlayer);

  const refreshResolved = async () => {
    try {
      const list = await api.alerts('resolved');
      store.set({ resolved: Array.isArray(list) ? list : [], resolvedErr: null });
    } catch (e) {
      store.set({ resolvedErr: errText(e) });
    }
  };
  every(10000, refreshResolved);

  const refreshDetail = async () => {
    const id = store.s.selected;
    if (id == null) return;
    try {
      const d = await api.vehicle(id);
      if (String(store.s.selected) === String(id)) store.set({ detail: d, detailErr: null });
    } catch (e) {
      if (String(store.s.selected) === String(id)) store.set({ detailErr: `Данные ТС недоступны: ${errText(e)}` });
    }
  };
  every(2000, refreshDetail);

  // Поток подключается последним: mock отдаёт первое состояние синхронно, и автовыбор
  // инцидента должен застать уже объявленные функции опроса.
  api.connect({
    onState: (snap) => {
      if (snap && typeof snap === 'object') store.set({ snap });
    },
    onLink: (link) => store.set({ link }),
  });

  setInterval(() => store.set({ tick: Date.now() }), 1000);

  if (api.mode === 'mock' && reason === 'unavailable' && config.mock !== '1') {
    const probe = createHttpApi();
    setInterval(async () => {
      if (store.s.backendBack) return;
      try {
        const h = await probe.health();
        if (h && (h.status || h.stream)) store.set({ backendBack: true });
      } catch {}
    }, 10000);
  }

  // Первый инцидент открывается сам, чтобы карточка и схема рейса не пустовали; карта не перелетает.
  function autoSelect(s) {
    const first = incidents(s).find((x) => !x.cleared);
    if (!first) return;
    store.set({ autoSelected: true });
    select(first.key, { fly: false, open: false });
  }

  // Выбор ТС пользователем разворачивает свёрнутую карточку; автовыбор при загрузке — нет.
  function select(id, { fly = true, open = true } = {}) {
    const cur = store.s.selected;
    const next = id == null ? null : String(id);
    if (!store.s.autoSelected) store.set({ autoSelected: true });
    if (next != null && open && store.s.foldCard) store.set({ foldCard: false });
    if (next === (cur == null ? null : String(cur))) {
      if (next != null && fly) {
        flyPending = true;
        syncMap(store.s, new Set(['selected']));
      }
      return;
    }
    flyPending = next != null && fly;
    store.set({ selected: next, detail: null, detailErr: null, whatif: null, ackErr: null });
    if (next != null) refreshDetail();
  }

  function setHotspot(key) {
    store.set({ hotspot: key });
    mapCtl?.setHotspot(hotspotByKey(store.s, key));
  }

  function openDialog(id) {
    const d = $(id);
    if (!d) return;
    store.set({ dialog: id });
    if (!d.open) d.showModal();
    DIALOG_RENDER[id]?.(store.s);
  }

  for (const d of document.querySelectorAll('dialog.dlg')) {
    d.addEventListener('close', () => {
      if (store.s.dialog === d.id) store.set({ dialog: null });
    });
    // Клик по подложке закрывает окно.
    d.addEventListener('click', (e) => {
      if (e.target === d) d.close();
    });
  }

  async function setAck(id, on) {
    store.set({ ackBusy: String(id), ackErr: null });
    try {
      const a = await (on ? api.ack(id) : api.unack(id));
      const snap = store.s.snap;
      if (snap?.alerts && a) {
        store.set({ snap: { ...snap, alerts: snap.alerts.map((x) => (String(x.id) === String(id) ? { ...x, ack_at: a.ack_at ?? null } : x)) } });
      }
    } catch (e) {
      const why = e?.status === 404 ? 'инцидент уже закрыт' : errText(e);
      store.set({ ackErr: `${on ? 'Не удалось взять в работу' : 'Не удалось снять отметку'}: ${why}` });
    } finally {
      store.set({ ackBusy: null });
    }
  }

  async function playerCall(fn) {
    store.set({ playerBusy: true });
    try {
      await fn();
      await refreshPlayer();
    } catch (e) {
      store.set({ replayerErr: errText(e) });
    } finally {
      store.set({ playerBusy: false });
    }
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    if (!el || el.disabled) return;
    const s = store.s;
    switch (el.dataset.act) {
      case 'select':
        select(el.dataset.id);
        break;
      case 'deselect':
        select(null);
        break;
      case 'fold': {
        const left = el.dataset.side === 'left';
        const off = !(left ? s.foldLeft : s.foldCard);
        store.set(left ? { foldLeft: off } : { foldCard: off });
        // Кнопка, по которой кликнули, исчезает вместе с панелью: фокус переходит на её пару.
        const target = off ? $(left ? 'fold-left' : 'fold-card') : document.querySelector(`#${left ? 'left-panel' : 'card'} [data-act="fold"]`);
        target?.focus();
        break;
      }
      case 'goto':
        store.set({ view: 'monitor' });
        select(el.dataset.id);
        break;
      case 'filter': {
        const r = el.dataset.risk;
        store.set({ filter: s.filter === r ? null : r });
        break;
      }
      case 'left-tab':
        store.set({ leftTab: el.dataset.tab });
        break;
      case 'unsched':
        store.set({ showUnsched: !s.showUnsched });
        break;
      case 'view':
        if (s.view !== el.dataset.view) store.set({ view: el.dataset.view });
        break;
      case 'focus':
        flyPending = true;
        syncMap(s, new Set(['selected']));
        break;
      case 'ctx':
        store.set({ showCtx: !s.showCtx });
        break;
      case 'fit':
        mapCtl?.fitAll();
        break;
      case 'hotspot':
        store.set({ view: 'monitor' });
        setHotspot(el.dataset.key);
        break;
      case 'hotspot-clear':
        setHotspot(null);
        break;
      case 'dialog':
        openDialog(el.dataset.dialog);
        break;
      case 'close':
        el.closest('dialog')?.close();
        break;
      case 'ack':
        setAck(el.dataset.id, true);
        break;
      case 'unack':
        setAck(el.dataset.id, false);
        break;
      case 'speed': {
        const sp = +el.dataset.speed;
        store.set({ playerSpeed: sp });
        if (s.replayer && (s.replayer.state === 'running' || s.replayer.state === 'paused')) playerCall(() => api.replayer.speed(sp));
        break;
      }
      case 'pause':
        playerCall(() => api.replayer.pause());
        break;
      case 'resume':
        playerCall(() => api.replayer.resume());
        break;
      case 'stop':
        playerCall(() => api.replayer.stop());
        break;
      case 'reload': {
        const u = new URL(location.href);
        u.searchParams.delete('mock');
        location.href = u.toString();
        break;
      }
    }
  });

  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (form.id === 'player') {
      e.preventDefault();
      const t = $('pl-time').value || '07:40';
      playerCall(() => api.replayer.start({ speed: store.s.playerSpeed, start_time: t, warmup_min: 20 }));
      $('pl-time')._touched = false;
      return;
    }
    if (form.id === 'wi-form') {
      e.preventDefault();
      const s = store.s;
      if (s.selected == null) return;
      const scenario = form.querySelector('input[name="sc"]:checked')?.value || 'extra_vehicle';
      const value = WHATIF_PARAM[scenario] ? +$('wi-v').value : null;
      const trId = /^\d+$/.test(String(s.selected)) ? +s.selected : s.selected;
      const tr = s.selected;
      store.set({ whatif: { tr_id: tr, scenario, value, loading: true } });
      api
        .whatif({ tr_id: trId, scenario, value })
        .then((res) => {
          if (String(store.s.selected) === String(tr)) store.set({ whatif: { tr_id: tr, scenario, value, res } });
        })
        .catch((err) => {
          if (String(store.s.selected) === String(tr)) store.set({ whatif: { tr_id: tr, scenario, value, error: errText(err) } });
        });
    }
  });

  document.addEventListener('input', (e) => {
    if (e.target.id === 'wi-v') {
      const sc = document.querySelector('#wi-form input[name="sc"]:checked')?.value;
      $('wi-o').value = `${e.target.value} ${WHATIF_PARAM[sc]?.unit || ''}`;
    }
    if (e.target.id === 'pl-time') e.target._touched = true;
  });

  document.addEventListener('change', (e) => {
    if (e.target.name === 'sc') setWhatifScenario(e.target.value);
  });

  // Стрелки переключают вкладки внутри tablist.
  document.addEventListener('keydown', (e) => {
    const tab = e.target.closest?.('[role="tab"]');
    if (tab && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
      const tabs = [...tab.parentElement.querySelectorAll('[role="tab"]')];
      const next = tabs[(tabs.indexOf(tab) + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.click();
      requestAnimationFrame(() => (next.isConnected ? next : tab.parentElement.querySelector(`[data-tab="${next.dataset.tab}"]`))?.focus());
      e.preventDefault();
      return;
    }
    if (e.key !== 'Escape' || document.querySelector('dialog[open]') || e.target.closest('input, textarea, select')) return;
    if (store.s.selected != null) select(null);
    else if (store.s.hotspot != null) setHotspot(null);
  });

  let resizeT = 0;
  window.addEventListener('resize', () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => {
      const box = $('rv-chart');
      if (box) box._sig = null;
      renderOpenDialog(store.s);
    }, 150);
  });
}

boot().catch((e) => {
  document.body.insertAdjacentHTML('afterbegin', `<p class="fatal">Дашборд не запустился: ${esc(errText(e))}</p>`);
  console.error(e);
});
