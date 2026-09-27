# Контекст сборки — корень репозитория:
#   docker build -f services/ml/Dockerfile -t transit-ml .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_NUM_THREADS=2

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/ml/requirements.txt /app/services/ml/requirements.txt
RUN pip install --retries 10 --timeout 120 -r /app/services/ml/requirements.txt

COPY libs/transit_core /app/libs/transit_core
COPY services/ml /app/services/ml
RUN pip install --no-deps /app/libs/transit_core /app/services/ml

ENV ARTIFACTS_DIR=/artifacts \
    MODEL_DIR=/artifacts/models/current \
    DATASET_DIR=/data

WORKDIR /app/services/ml
EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2)"

CMD ["uvicorn", "mlcore.api:app", "--host", "0.0.0.0", "--port", "8001"]
