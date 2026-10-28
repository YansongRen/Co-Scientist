FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Keep the image build independent from the host virtual environment.
COPY pyproject.toml README.md ./
COPY co_scientist ./co_scientist
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin coscientist \
    && mkdir -p /data \
    && chown -R coscientist:coscientist /app /data

COPY deployment/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

USER coscientist
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
