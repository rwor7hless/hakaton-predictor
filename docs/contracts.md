# Контракты между модулями

Единый источник правды для интерфейсов. Все времена — ISO-8601 без таймзоны
(`2026-01-06T06:35:00`), это «время потока» (время телеметрии), а не wall-clock.
Задержки — секунды, `+` опоздание, `−` опережение.

## Порты и сервисы (docker-compose)

| Сервис | Порт | Назначение |
|---|---|---|
| `backend` | 8000 HTTP, 9201 TCP | REST/WS API, приём NDTP, сопоставление с расписанием, алерты |
| `ml` | 8001 HTTP | ML-ядро: инференс, дообучение, информация о модели |
| `replayer` | 8090 HTTP | проигрывание исторического `traffic.csv` в NDTP-поток на `backend:9201` |
| `dashboard` | 8080 HTTP | nginx: SPA + прокси `/api`, `/ws` → backend, `/replayer/` → replayer, `/ml/` → ml |
| `emulator` | 18080 HTTP | эмулятор организаторов (`ndtp-telemetry-emulator:1.0`), шлёт на `backend:9201` |

## 1. NDTP-библиотека `libs/ndtp` (пакет `ndtp`)

```python
from ndtp import (
    NavCell, Cell, Frame, ConnRequest, NavPacket,
    FrameDecoder, encode_conn_request, encode_nav_packet, encode_result, crc16_modbus,
)

@dataclass
class NavCell:            # G6CellNav00, type 0, 26 байт
    timestamp: int        # unix seconds (UTC-наивное «время потока», как в CSV)
    lon: float            # градусы со знаком (E=+)
    lat: float            # градусы со знаком (N=+)
    valid: bool           # extraDopBit7
    speed_avg: int = 0    # км/ч
    speed_max: int = 0
    course: int = 0
    track: int = 0
    altitude: int = 0
    nsat: int = 0
    pdop: int = 0
    bat_voltage: int = 0
    alarm: bool = False; sos: bool = False; ...  # остальные extraDop-биты

@dataclass
class NavPacket:          # NPH service 1 (NAVDATA), type 101 REALTIME или 100 HISTORY
    unit_id: int          # NPL peerAddress
    request_id: int
    history: bool         # True для type 100
    nav: NavCell | None
    cells: list[Cell]     # прочие ячейки (type, number, raw bytes / parsed dict)

@dataclass
class ConnRequest:        # NPH service 0, type 100
    unit_id: int; proto_high: int; proto_low: int; max_packet_size: int; request_id: int

decoder = FrameDecoder()          # потоковый, по одному на TCP-соединение
msgs = decoder.feed(data: bytes)  # -> list[ConnRequest | NavPacket | Frame]; битые кадры пропускает (resync по 0x7E7E, проверка CRC)
decoder.stats                     # dict: frames_ok, crc_errors, resyncs, unknown
encode_conn_request(unit_id, request_id) -> bytes
encode_nav_packet(unit_id, request_id, nav: NavCell, history=False, extra_cells=()) -> bytes
encode_result(unit_id, request_id, service_id, error=0) -> bytes   # NPH_RESULT (ответ сервера)
```

Сервер: `ndtp.server.NdtpServer(host, port, on_message: Callable[[ConnInfo, msg], Awaitable|None], on_disconnect)`
на asyncio; отвечает `NPH_RESULT` на запросы с флагом `request`.

## 2. Backend REST/WS API (для дашборда)

`GET /api/health`
```json
{"status": "ok|degraded",
 "stream": {"state": "live|stale|no_data", "connected_units": 12, "known_units": 30,
            "last_packet_at": "2026-01-06T06:35:02", "stream_time": "2026-01-06T06:35:02",
            "packets_total": 12345, "packets_per_s": 18.5, "crc_errors": 0},
 "ml": {"state": "ok|fallback", "model_version": "2026-09-25-1", "last_latency_ms": 7.1},
 "uptime_s": 1234.5}
```

`GET /api/state` — снимок для дашборда (то же приходит по WS):
```json
{"stream_time": "...", "generated_at": "...",
 "summary": {"vehicles_total": 13, "vehicles_active": 11, "red": 2, "yellow": 3, "green": 6, "grey": 2,
             "online_mae_s": 71.2, "online_n": 40, "mean_lead_min": 11.4},
 "vehicles": [VehicleState], "alerts": [Alert], "recent_resolved": [Alert],
 "health": {...как /api/health}}
```

`summary.red` / `summary.yellow` считают ТС по статусу `risk`, то есть по уровню открытого
инцидента. `recent_resolved` — инциденты, закрытые за последние 5 мин потока.

`VehicleState`
```json
{"tr_id": 131672, "unit_id": 664030, "route_name": "Новокосино — Перово",
 "lat": 55.74, "lon": 37.85, "heading": 120, "speed_kmh": 23.0,
 "last_seen": "...", "gps_age_s": 4.0,
 "status": "moving|stopped|stale|offline|no_schedule|out_of_service",
 "current_delay_s": 95.0,
 "last_stop": {"stop_id": "53700172812", "name": "Суздальская ул., д.15А", "planned": "...", "actual": "...", "delay_s": 95.0},
 "next_stop": {"stop_id": "...", "name": "...", "planned": "...", "eta": "..."},
 "prediction": Prediction | null,
 "risk": "red|yellow|green|grey",
 "risk_pending": {"count": 1, "need": 2} | null,
 "prediction_held": {"since": "...", "reason": "trip_end|long_segment", "resume_at": "..." | null} | null,
 "degraded": false}
```

`risk` — статус ТС для карты и сводки: уровень открытого инцидента или `green`, если
инцидента нет. Риск отдельного прогноза — в `prediction.risk`. `risk_pending` — прогноз
уже в зоне «Внимание», но инцидент ещё не заведён: сколько раз подряд из нужных.
`prediction_held` — в окне 10–15 мин нет плановой остановки (до конечной меньше 10 мин или
длинный перегон): `prediction` — последний выданный прогноз, он остаётся, пока ТС не
проедет его цель; новых прогнозов в это время нет.

`Prediction`
```json
{"issued_at": "2026-01-06T06:35:00", "target_stop_id": "53700172861", "target_stop_name": "Перовская ул., д.70",
 "target_planned": "2026-01-06T06:47:00", "lead_min": 12.0,
 "delay_s": 184.0, "delay_q10_s": 90.0, "delay_q90_s": 260.0, "p_late": 0.78, "p_early": 0.02,
 "risk": "red",
 "causes": [{"code": "dwell_off_stop", "text": "Простой 3 мин вне остановки", "weight": 0.6}],
 "segment": {"from_stop": "Кетчерская ул., д.4Б", "to_stop": "Перовская ул., д.70",
             "from_lat": 55.74, "from_lon": 37.82, "to_lat": 55.74, "to_lon": 37.81},
 "recommendations": ["Выпустить резервное ТС с конечной ...", "..."],
 "model": "ensemble|catboost|fallback"}
```

`Alert`
```json
{"id": "131672-7", "tr_id": 131672, "route_name": "...", "created_at": "...", "updated_at": "...",
 "risk": "red|yellow", "kind": "late|early", "prediction": Prediction,
 "status": "active|resolved", "actual_delay_s": null, "error_s": null, "ack_at": null,
 "opened": {"at": "...", "delay_s": 110.0, "target_stop_name": "...", "target_planned": "...", "reason": Reason},
 "reason": Reason | null,
 "peak": {"at": "...", "delay_s": 216.0, "target_stop_name": "..."},
 "history": [["2026-01-06T07:05:00", 110.0, "yellow"], ...],
 "closing": {"calm_since": "..." | null, "calm_s": 60.0, "need_s": 180.0, "band_s": 60.0},
 "reopen_count": 0, "reopened_at": null,
 "resolved_at": null, "resolved_reason": "calm|trip_end|no_data|no_forecast" | null}
```

`Reason` — какое правило светофора сработало:
`{"risk": "red|yellow", "rule": "delay_late|p_late|delay_early|p_early", "value": 189.0, "threshold": 180.0}`.

Жизненный цикл инцидента (`services/backend/backend/fleet.py`, `_track_incident`):

* заводится, когда прогноз в зоне «Внимание» 2 раза подряд; прогноз «Критично» заводит его сразу;
* уровень открытого инцидента меняется («Внимание» ↔ «Критично»), только если новый уровень
  подтверждён 2 прогнозами подряд; пока инцидент открыт, статус ТС не ниже «Внимание»;
* закрывается (`calm`), когда прогноз 3 мин потока подряд зелёный и в пределах ±1 мин;
  иначе — когда прогноз для ТС больше не строится: рейс завершён (`trip_end`), нет данных
  от ТС (`no_data`) или нет цели в окне 10–15 мин после удержанного прогноза (`no_forecast`);
* если у той же машины риск вернулся в течение 10 мин после закрытия по `calm`,
  продолжается прежний инцидент (`reopen_count` растёт).

`ack_at` — время потока, когда диспетчер взял инцидент в работу; `null`, если не взят.
Сохраняется и после закрытия инцидента.

Остальные эндпоинты:

* `GET /api/vehicles` → `[VehicleState]`
* `GET /api/vehicles/{tr_id}` → `VehicleState` + `"track": [[lon, lat, "ts"], ...]` (30 мин),
  `"upcoming": [{"stop_id", "name", "planned", "eta", "lat", "lon"}]` (не меньше 10 и до цели прогноза включительно),
  `"delay_history": [{"t": "...", "delay_s": 12.0, "kind": "actual|estimate|predicted"}]` —
  `actual` — факт на остановке, `estimate` — оценка трекера между остановками, `predicted` —
  прогноз с `t` = плановое время цели и полями `issued_at`, `lead_min`,
  `current_at_issue_s` (отклонение в момент выдачи), `actual_s` (факт на цели, когда наступил)
* `GET /api/routes` → `[{"tr_id", "route_name", "stops": [{"stop_id", "name", "lat", "lon"}], "path": [[lon, lat], ...],
  "segments": [{"from_idx": 0, "to_idx": 1, "risk": "green|yellow|red"}]}]`
* `GET /api/alerts?status=active|resolved|all` → `[Alert]`
* `POST /api/alerts/{id}/ack` → `Alert` с `ack_at`; `DELETE` снимает отметку; 404, если активного инцидента нет
* `GET /api/metrics` →
  `{"online": {"mae_s", "n", "horizon_ok_share", "mean_lead_min", "retro_predictions"},
    "latency": {"ingest_to_prediction_ms_p50", "ingest_to_prediction_ms_p95", "ml_predict_ms_p50", "ml_predict_ms_p95"},
    "throughput": {"packets_per_s", "queue_len", "predictions_total"},
    "offline": {"test_mae_s", "baseline_mae_s", "zero_mae_s"}}`
* `POST /api/whatif` `{"tr_id": 131672, "scenario": "extra_vehicle|dwell_cut|signal_priority", "value": 60}` →
  `{"base": Prediction, "delay_s_after": 40.0, "risk_after": "green", "explanation": "..."}`
* `WS /ws` → раз в 1 с `{"type": "state", "data": <как /api/state>}`

## 3. Replayer API (через nginx: `/replayer/api/...`)

* `GET /api/status` → `{"state": "idle|running|paused|finished", "speed": 20, "stream_time": "...", "start_time": "06:30", "units": 30, "sent": 1234, "connected": 30}`
* `POST /api/start` `{"speed": 20, "start_time": "06:30", "warmup_min": 20}`
* `POST /api/pause`, `POST /api/resume`, `POST /api/stop`, `POST /api/speed` `{"speed": 60}`

## 4. ML API (backend → ml)

* `POST /v1/predict` `{"items": [{"id": "131672", "features": {"est_now": 95.0, ...}}]}` →
  `{"model_version": "...", "latency_ms": 3.2,
    "items": [{"id", "delay_s", "q10_s", "q90_s", "p_late", "p_early",
               "contributions": [{"feature": "est_now", "value": 95.0, "shap": 41.2}]}]}`
* `GET /v1/model` → версия, признаки, метрики, состав ансамбля
* `POST /v1/train` → запуск дообучения в фоне, `GET /v1/train/{job_id}` → статус
* `GET /health`

## 5. Дополнение: проблемные участки

`GET /api/hotspots?top=15` →
```json
[{"tr_id": 131672, "route_name": "...", "from_stop": "Кетчерская ул., д.4Б", "to_stop": "Перовская ул., д.70",
  "from_lat": 55.74, "from_lon": 37.82, "to_lat": 55.74, "to_lon": 37.81,
  "n": 24, "risk_share": 0.62, "mean_delay_s": 141.3, "max_delay_s": 260.0,
  "top_causes": [{"code": "slow_segment", "text": "низкая скорость", "count": 9}]}]
```
Отсортировано по значимости. `path` в `/api/routes` — реальная геометрия по GPS-треку
(десятки–сотни точек), а `segments[].from_idx/to_idx` — индексы в `stops`.
