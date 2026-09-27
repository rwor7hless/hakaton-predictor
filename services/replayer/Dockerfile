# Контекст сборки — корень репозитория:
#   docker build -f services/replayer/Dockerfile -t replayer .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY services/replayer/requirements.txt /app/services/replayer/requirements.txt
RUN pip install --retries 10 --timeout 120 -r /app/services/replayer/requirements.txt

COPY libs/ndtp /app/libs/ndtp
COPY services/replayer /app/services/replayer
RUN pip install --no-deps /app/libs/ndtp /app/services/replayer

ENV TRAFFIC_CSV=/data/validate/traffic.csv \
    NDTP_HOST=backend \
    NDTP_PORT=9201 \
    REPLAY_SPEED=20 \
    REPLAY_START=06:30 \
    REPLAY_WARMUP_MIN=20 \
    REPLAY_AUTOSTART=1

EXPOSE 8090

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)"

CMD ["uvicorn", "replayer.app:app", "--host", "0.0.0.0", "--port", "8090"]
