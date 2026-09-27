# Контекст сборки — корень репозитория:
#   docker build -f services/backend/Dockerfile -t transit-backend .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY services/backend/requirements.txt /app/services/backend/requirements.txt
RUN pip install --retries 10 --timeout 120 -r /app/services/backend/requirements.txt

COPY libs/ndtp /app/libs/ndtp
COPY libs/transit_core /app/libs/transit_core
COPY services/backend /app/services/backend
RUN pip install --no-deps /app/libs/ndtp /app/libs/transit_core /app/services/backend

ENV SCHEDULE_CSV=/data/validate/schedule_plan.csv \
    UNITS_CSV=/artifacts/reference/units.csv \
    FALLBACK_JSON=/artifacts/reference/fallback.json \
    ROUTE_PATHS_JSON=/artifacts/reference/route_paths.json \
    ROUTE_GEOMETRY_JSON=/artifacts/reference/route_geometry.json \
    STOP_NAMES_JSON=/artifacts/reference/stop_names.json \
    ML_URL=http://ml:8001 \
    NDTP_PORT=9201

EXPOSE 8000 9201

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
