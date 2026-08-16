FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

LABEL org.opencontainers.image.source="https://github.com/ray-cys/metafusion" \
      org.opencontainers.image.description="Metadata and asset generator for Plex and Kometa"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --require-hashes -r requirements.lock

COPY --chown=10001:10001 . /app
RUN mkdir -p /config /config/logs /config/cache /kometa \
    && chown -R 10001:10001 /config /kometa

USER 10001:10001

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "healthcheck.py"]
CMD ["python", "metafusion.py"]
