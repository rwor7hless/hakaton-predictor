PY ?= .venv/bin/python

.PHONY: up down logs test train reference routes openapi docs bench emulator

up:            ## собрать и поднять систему
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100 backend ml replayer

emulator:      ## поднять эмулятор организаторов (образ должен быть загружен docker load)
	docker compose --profile emulator up -d emulator

test:
	$(PY) -m pytest -q tests

train:         ## признаки → честная валидация → финальные модели → artifacts/submission.csv
	cd services/ml && PYTHONPATH=. ../../$(PY) -m mlcore.train

reference:     ## справочники backend (unit_id ↔ tr_id, fallback-профиль)
	$(PY) scripts/build_reference.py

routes:        ## геометрия маршрутов по дорогам (OSRM) и названия остановок без адреса (Nominatim); ответы кэшируются
	$(PY) scripts/build_routes.py
	$(PY) scripts/build_stop_names.py

openapi:
	$(PY) scripts/export_openapi.py

docs: openapi
	$(PY) -m sphinx -q -b html docs docs/_build/html

bench:
	$(PY) scripts/benchmark.py
