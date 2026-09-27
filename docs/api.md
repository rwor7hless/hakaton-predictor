# API

Живая документация Swagger доступна у каждого сервиса:

| Сервис | Swagger UI | OpenAPI JSON |
|---|---|---|
| Backend | <http://localhost:8000/docs> | [backend.json](_static/openapi/backend.json) |
| ML-ядро | <http://localhost:8001/docs> | [ml.json](_static/openapi/ml.json) |
| Replayer | <http://localhost:8090/docs> | [replayer.json](_static/openapi/replayer.json) |

Статические версии спецификаций (без запуска сервисов):
[backend](_static/swagger.html?spec=openapi/backend.json) ·
[ML](_static/swagger.html?spec=openapi/ml.json) ·
[replayer](_static/swagger.html?spec=openapi/replayer.json).

## Основные эндпоинты backend

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/health` | состояние потока (live/stale/no_data), ML (ok/fallback), счётчики |
| GET | `/api/state` | снимок для дашборда: сводка, ТС, активные инциденты |
| WS | `/ws` | тот же снимок раз в секунду |
| GET | `/api/vehicles/{tr_id}` | трек за 30 мин, ближайшие остановки, история отклонения |
| GET | `/api/routes` | маршрутная сеть и риск по участкам |
| GET | `/api/alerts?status=active\|resolved\|all` | инциденты |
| POST, DELETE | `/api/alerts/{id}/ack` | взять активный инцидент в работу / снять отметку (`ack_at`) |
| GET | `/api/metrics` | онлайн-MAE, соблюдение горизонта, задержки, пропускная способность |
| POST | `/api/whatif` | эффект мер диспетчера: `extra_vehicle`, `dwell_cut`, `signal_priority` |
| POST | `/api/telemetry` | приём телеметрии в JSON (альтернатива NDTP) |

## ML-ядро

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/v1/predict` | пакетный онлайн-прогноз: задержка, q10/q90, p_late/p_early, SHAP-вклады |
| POST | `/v1/predict/offline` | прогноз в режиме сабмита (с `cur_dev`) |
| GET | `/v1/model` | версия, признаки, метрики, состав ансамбля |
| POST | `/v1/train`, GET `/v1/train/{id}` | дообучение в фоне с горячей заменой |
| GET | `/metrics` | латентность инференса p50/p95 |

## Пример

```bash
curl -s localhost:8000/api/alerts | jq '.[0].prediction | {target_stop_name, delay_s, p_late, causes}'
curl -s -X POST localhost:8000/api/whatif -H 'Content-Type: application/json' \
     -d '{"tr_id": 131672, "scenario": "dwell_cut", "value": 90}'
```
