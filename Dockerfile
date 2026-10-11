# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY requirements.lock pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    LOG_FORMAT=json \
    DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/tenderbuddy.db \
    PORT=8004
RUN groupadd --system app && useradd --system --gid app --home /app app \
 && mkdir -p /data /app && chown app:app /data /app
WORKDIR /app
COPY --from=build /dist/*.whl /tmp/
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
 && pip install --no-cache-dir --no-deps /tmp/*.whl \
 && rm -f /tmp/*.whl /tmp/requirements.lock
USER app
VOLUME ["/data"]
EXPOSE 8004
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=4)" "http://127.0.0.1:${PORT}/healthz"
CMD ["sh", "-c", "tenderbuddy migrate && exec tenderbuddy serve --host 0.0.0.0 --port ${PORT} --forwarded-allow-ips '*'"]
