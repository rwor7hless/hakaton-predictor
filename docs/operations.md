# Запуск и инструкция для жюри

## Запуск одной командой

```bash
# 1. Поднять систему: датасет уже лежит в ./data/dataset
docker compose up -d --build

# 2. Открыть дашборд: http://localhost:8080
```

Если датасет находится отдельно, задайте `DATASET_DIR=/абсолютный/путь/к/dataset`
в `.env` перед запуском. Если порт 8080 занят, задайте `DASHBOARD_PORT=8081` в
`.env` и откройте <http://localhost:8081>. Поток из исторического датасета стартует
сам (replayer, ×10, с 06:30 дня validate, предварительно прогревая трекеры 20
минутами истории). Состояние сервисов после запуска видно в `docker compose ps`.

## Как подать поток

**Исторический датасет (по умолчанию).** Сервис `replayer` проигрывает
`validate/traffic.csv` как 30 NDTP-терминалов (по TCP-соединению на ТС) в `backend:9201`.
Управление — на дашборде (кнопка «Поток» в шапке) или через API:

```bash
curl -X POST localhost:8090/api/start -H 'Content-Type: application/json' \
     -d '{"speed": 30, "start_time": "07:00", "warmup_min": 20}'
curl -X POST localhost:8090/api/pause    # /resume, /stop
curl -X POST localhost:8090/api/speed -d '{"speed": 60}' -H 'Content-Type: application/json'
```

**Эмулятор организаторов.**

Архив образа эмулятора предоставляется отдельно и не входит в репозиторий:

```bash
docker load -i /путь/к/ndtp-telemetry-emulator.tar
make emulator
curl -X POST localhost:18080/api/config -H 'Content-Type: application/json' -d '{
  "targetHost": "backend", "targetPort": 9201,
  "units": [{"unitId": 1166336, "intervalMs": 2000, "autoGenerate": true, "cells": []}]}'
```

Терминалы эмулятора без расписания видны на дашборде как ТС «без расписания»: пакеты
разбираются, CRC проверяется, координаты отображаются. Для проверки деградации
приостановите поток (`docker compose pause replayer`) — система продолжит работу
по последнему состоянию. Возобновите его через `docker compose unpause replayer`;
текущая позиция потока сохранится.

## Где смотреть

| Что | Где |
|---|---|
| Дашборд диспетчера | <http://localhost:8080> |
| Прогнозы и алерты (API) | <http://localhost:8000/api/state>, <http://localhost:8000/api/alerts> |
| Swagger backend / ML / replayer | <http://localhost:8000/docs>, <http://localhost:8001/docs>, <http://localhost:8090/docs> |
| Метрики (онлайн-MAE, горизонт, задержки) | <http://localhost:8000/api/metrics>, <http://localhost:8001/metrics> |
| Документация (эта) | <http://localhost:8080/docs-site/> |
| Сабмит v7 (сообщённый score 1.00000) | `artifacts/submission-v7.csv` |
| Сабмит исходного ансамбля | `artifacts/submission.csv` |

## Проверка горизонта 10–15 минут

Каждый прогноз содержит `issued_at` (время потока, когда он сделан), `target_planned`
(плановое время события) и `lead_min`. `/api/metrics` → `online.horizon_ok_share` —
доля прогнозов с упреждением 10–15 мин (1.0), `retro_predictions` — прогнозы,
сделанные после фактического события (0).

## Конфигурация

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `DATASET_DIR` | `./data/dataset` | каталог датасета (монтируется в `/data`) |
| `REPLAY_SPEED` | 10 | ускорение проигрывания |
| `REPLAY_START` | 06:30 | с какого времени дня проигрывать |
| `PREDICT_EVERY_S` | 30 | период прогнозного цикла по часам потока, с |
| `DASHBOARD_PORT` | 8080 | порт дашборда на хосте |
| `COMPOSE_SUBNET` | 10.213.77.0/24 | подсеть для `compose.subnet.yml` (если в Docker закончились пулы адресов) |
