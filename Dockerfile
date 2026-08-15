FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/ray-cys/metafusion" \
      org.opencontainers.image.description="Metadata and asset generator for Plex and Kometa"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools \
    && python -m pip install -r requirements.txt

COPY . /app
RUN mkdir -p /config /config/logs /config/cache

STOPSIGNAL SIGTERM
CMD ["python", "metafusion.py"]
