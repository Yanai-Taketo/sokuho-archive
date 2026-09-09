# Minimal image for the watch daemon. No dependencies to install: the archiver
# is standard library only, which is the point.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 sokuho
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    SOKUHO_DATA_DIR=/data

RUN mkdir -p /data && chown sokuho:sokuho /data
VOLUME ["/data"]
USER sokuho

# A container that cannot reach the feed should be visibly unhealthy rather
# than quietly idle.
HEALTHCHECK --interval=5m --timeout=30s --start-period=2m \
  CMD python -m sokuho_archive status --stale-hours 1 || exit 1

ENTRYPOINT ["python", "-m", "sokuho_archive"]
CMD ["watch", "--interval", "60"]
