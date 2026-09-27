const qs = new URLSearchParams(location.search);

function wsUrlFrom(apiBase) {
  if (qs.get('ws')) return qs.get('ws');
  if (apiBase) {
    const u = new URL(apiBase, location.href);
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    u.pathname = u.pathname.replace(/\/$/, '') + '/ws';
    return u.toString();
  }
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
}

const apiBase = (qs.get('api') || '').replace(/\/$/, '');

export const config = {
  apiBase,
  replayerBase: (qs.get('replayer') || '/replayer').replace(/\/$/, ''),
  wsUrl: wsUrlFrom(apiBase),
  mock: qs.get('mock'),
  pollMs: 2000,
};

export class HttpError extends Error {
  constructor(status, body) {
    super(`HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

export async function request(url, { method = 'GET', body, timeout = 4000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  try {
    const res = await fetch(url, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
      cache: 'no-store',
    });
    const text = await res.text();
    if (!res.ok) throw new HttpError(res.status, text);
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      throw new Error('Ответ не в формате JSON');
    }
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('Превышено время ожидания');
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function absolute(base, path) {
  if (/^https?:\/\//.test(base) || base.startsWith('/')) return base + path;
  return new URL(base + path, location.href).toString();
}

export function createHttpApi(cfg = config) {
  const a = (p) => (cfg.apiBase ? cfg.apiBase : '') + '/api' + p;
  const r = (p) => absolute(cfg.replayerBase, '/api' + p);
  const api = {
    mode: 'live',
    health: () => request(a('/health'), { timeout: 3000 }),
    state: () => request(a('/state')),
    vehicles: () => request(a('/vehicles')),
    vehicle: (id) => request(a('/vehicles/' + encodeURIComponent(id))),
    routes: (geometry = true) => request(a('/routes' + (geometry ? '' : '?geometry=false')), { timeout: geometry ? 20000 : 4000 }),
    alerts: (status = 'active') => request(a('/alerts?status=' + encodeURIComponent(status))),
    metrics: () => request(a('/metrics')),
    hotspots: (top = 15) => request(a('/hotspots?top=' + encodeURIComponent(top))),
    whatif: (body) => request(a('/whatif'), { method: 'POST', body, timeout: 10000 }),
    ack: (id) => request(a('/alerts/' + encodeURIComponent(id) + '/ack'), { method: 'POST' }),
    unack: (id) => request(a('/alerts/' + encodeURIComponent(id) + '/ack'), { method: 'DELETE' }),
    replayer: {
      status: () => request(r('/status'), { timeout: 3000 }),
      start: (body) => request(r('/start'), { method: 'POST', body }),
      pause: () => request(r('/pause'), { method: 'POST', body: {} }),
      resume: () => request(r('/resume'), { method: 'POST', body: {} }),
      stop: () => request(r('/stop'), { method: 'POST', body: {} }),
      speed: (speed) => request(r('/speed'), { method: 'POST', body: { speed } }),
    },
    connect: (handlers) => connectLive(cfg, api, handlers),
  };
  return api;
}

function connectLive(cfg, api, { onState, onLink }) {
  let ws = null;
  let closed = false;
  let retry = 0;
  let pollTimer = null;
  let reconnectTimer = null;
  let lastMsg = 0;
  let polling = false;
  const link = { transport: 'connecting', lastAt: 0, error: null, wsOpen: false };

  const emit = (patch) => {
    Object.assign(link, patch);
    onLink({ ...link });
  };

  const deliver = (data, via) => {
    lastMsg = Date.now();
    onState(data);
    emit({ transport: via, lastAt: lastMsg, error: null });
  };

  async function pollOnce() {
    if (polling) return;
    polling = true;
    try {
      const s = await api.state();
      if (!closed && !(ws && ws.readyState === 1 && Date.now() - lastMsg < 3000)) deliver(s, 'poll');
    } catch (e) {
      if (!ws || ws.readyState !== 1) emit({ transport: 'down', error: e.message });
    } finally {
      polling = false;
    }
  }

  function startPolling() {
    if (pollTimer || closed) return;
    pollOnce();
    pollTimer = setInterval(pollOnce, cfg.pollMs);
  }

  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  function scheduleReconnect() {
    if (closed || reconnectTimer) return;
    const delay = Math.min(10000, 1000 * 2 ** retry);
    retry = Math.min(retry + 1, 6);
    emit({ wsOpen: false, retryInMs: delay });
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      open();
    }, delay);
  }

  function open() {
    if (closed) return;
    try {
      ws = new WebSocket(cfg.wsUrl);
    } catch {
      ws = null;
      startPolling();
      scheduleReconnect();
      return;
    }
    ws.onopen = () => {
      retry = 0;
      emit({ wsOpen: true });
    };
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      const data = msg && msg.type === 'state' ? msg.data : msg && msg.stream_time !== undefined && msg.vehicles ? msg : null;
      if (!data) return;
      stopPolling();
      deliver(data, 'ws');
    };
    ws.onclose = () => {
      ws = null;
      if (closed) return;
      startPolling();
      scheduleReconnect();
    };
    ws.onerror = () => {
      try {
        ws && ws.close();
      } catch {}
    };
  }

  const watchdog = setInterval(() => {
    if (closed) return;
    if (!ws || ws.readyState !== 1 || Date.now() - lastMsg > 4000) startPolling();
  }, 1000);

  startPolling();
  open();

  return () => {
    closed = true;
    clearInterval(watchdog);
    clearTimeout(reconnectTimer);
    stopPolling();
    try {
      ws && ws.close();
    } catch {}
  };
}
