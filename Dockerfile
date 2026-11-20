FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 RADAR_DB=/data/radar.duckdb PORT=8040 FORWARDED_ALLOW_IPS=127.0.0.1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY dbt ./dbt
COPY evals ./evals
COPY requirements.lock ./
RUN pip install --no-cache-dir -c requirements.lock . && useradd -u 10001 -m radar && mkdir -p /data && chown radar /data
USER 10001
EXPOSE 8040
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8040/health').status==200 else 1)"
CMD ["sh", "-c", "uvicorn 'radar.web:create_app' --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
