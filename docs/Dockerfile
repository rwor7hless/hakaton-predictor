# Сборка Sphinx-документации; контейнер копирует готовый сайт в том и завершается.
#   docker build -f docs/Dockerfile -t transit-docs .
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 NDTP_PORT=0
RUN pip install --retries 10 --timeout 120 "sphinx>=7" sphinx-rtd-theme myst-parser numpy pandas fastapi pydantic httpx "uvicorn[standard]"
WORKDIR /src
COPY libs libs
COPY services/backend services/backend
COPY services/ml/mlcore services/ml/mlcore
COPY services/replayer services/replayer
COPY docs docs
COPY README.md README.md
RUN sphinx-build -q -b html docs /site

FROM alpine:3.20
COPY --from=build /site /site
CMD ["sh", "-c", "rm -rf /out/* && cp -r /site/. /out/ && echo docs ready"]
