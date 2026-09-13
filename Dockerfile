# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Stage 1: build the gotohp CLI (no Wails/Node needed with the `cli` build tag)
# ---------------------------------------------------------------------------
ARG GO_IMAGE=golang:1-bookworm
FROM ${GO_IMAGE} AS gotohp-build
ARG GOTOHP_REPO=https://github.com/xob0t/gotohp.git
ARG GOTOHP_REF=main
ENV GOTOOLCHAIN=auto \
    CGO_ENABLED=0 \
    GOFLAGS=-buildvcs=false
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch "${GOTOHP_REF}" "${GOTOHP_REPO}" . \
 || (git clone "${GOTOHP_REPO}" . && git checkout "${GOTOHP_REF}")
RUN go build -tags cli -trimpath -ldflags="-w -s" -o /out/gotohp-cli . \
 && /out/gotohp-cli version || true

# ---------------------------------------------------------------------------
# Stage 2: the sidecar itself
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="immich-gphotos-sidecar" \
      org.opencontainers.image.description="Backs up Immich originals, sidecar metadata and albums to Google Photos via gotohp" \
      org.opencontainers.image.source="https://github.com/xob0t/gotohp"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    STATE_DIR=/state \
    SIDECAR_DIR=/sidecar \
    WORK_DIR=/work \
    GOTOHP_BIN=/usr/local/bin/gotohp-cli \
    GOTOHP_CONFIG=/config/gotohp.config \
    XDG_CONFIG_HOME=/config

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libimage-exiftool-perl \
      ca-certificates \
      tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=gotohp-build /out/gotohp-cli /usr/local/bin/gotohp-cli
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY app ./app

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/gotohp-cli \
 && mkdir -p /state /sidecar /work /config \
 && chmod 0777 /state /sidecar /work /config

VOLUME ["/state", "/sidecar", "/work", "/config"]

HEALTHCHECK --interval=10m --timeout=60s --start-period=30s --retries=3 \
  CMD python -m app.main stats >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["daemon"]
