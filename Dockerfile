# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# The uploader is the pure-python gpmc library (https://github.com/xob0t/gpmc),
# so there is no compiler stage any more: `pip install -r requirements.txt`
# brings the Google Photos client along with the rest of the dependencies.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# uploader upstream: https://github.com/xob0t/gpmc
LABEL org.opencontainers.image.title="immich-gphotos-sidecar" \
      org.opencontainers.image.description="Backs up Immich originals, sidecar metadata and albums to Google Photos via gpmc" \
      org.opencontainers.image.source="https://github.com/nmt3325/immich-gphotos-sidecar" \
      org.opencontainers.image.url="https://github.com/nmt3325/immich-gphotos-sidecar"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/config \
    STATE_DIR=/state \
    SIDECAR_DIR=/sidecar \
    WORK_DIR=/work \
    GPMC_CACHE_DIR=/config

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libimage-exiftool-perl \
      ca-certificates \
      tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY app ./app

RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /state /sidecar /work /config \
 && chmod 0777 /state /sidecar /work /config

VOLUME ["/state", "/sidecar", "/work", "/config"]

HEALTHCHECK --interval=10m --timeout=60s --start-period=30s --retries=3 \
  CMD python -m app.main stats >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["daemon"]
