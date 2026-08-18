FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

ARG METAFUSION_VERSION=development
ARG METAFUSION_COMMIT=unknown

LABEL org.opencontainers.image.source="https://github.com/ray-cys/metafusion" \
      org.opencontainers.image.description="Metadata and asset generator for Plex and Kometa" \
      org.opencontainers.image.url="https://github.com/ray-cys/metafusion" \
      org.opencontainers.image.documentation="https://github.com/ray-cys/metafusion#readme" \
      org.opencontainers.image.licenses="LicenseRef-All-Rights-Reserved" \
      org.opencontainers.image.version="${METAFUSION_VERSION}" \
      org.opencontainers.image.revision="${METAFUSION_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    METAFUSION_VERSION="${METAFUSION_VERSION}" \
    METAFUSION_COMMIT="${METAFUSION_COMMIT}"

WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --require-hashes -r requirements.lock

COPY --chown=10001:10001 . /app
RUN mkdir -p /config /config/logs /config/cache /kometa \
    && chown -R 10001:10001 /config /kometa

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker_entrypoint.py", "--healthcheck", "python", "healthcheck.py"]
ENTRYPOINT ["python", "/app/docker_entrypoint.py"]
CMD ["python", "metafusion.py"]
