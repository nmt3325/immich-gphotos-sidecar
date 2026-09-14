#!/usr/bin/env bash
# Entry point: prepares volumes/config, then dispatches to the sidecar CLI.
#   daemon (default) | run | doctor | stats | version
#   gpmc <args...>    -> raw gpmc CLI (e.g. `gpmc /work/foo --recursive`)
#   anything else     -> executed verbatim (sh, bash, python, ...)
set -euo pipefail

STATE_DIR="${STATE_DIR:-/state}"
SIDECAR_DIR="${SIDECAR_DIR:-/sidecar}"
WORK_DIR="${WORK_DIR:-/work}"
GPMC_CACHE_DIR="${GPMC_CACHE_DIR:-/config}"

# gpmc keeps its library cache in ~/.gpmc, so point HOME at the persistent
# volume. That keeps the hash cache across container restarts and makes the raw
# `gpmc` CLI use the same place as the sidecar.
export HOME="$GPMC_CACHE_DIR"

# gpmc reads GP_AUTH_DATA when no auth data is passed explicitly; keep it in
# sync with the sidecar variable (and the legacy gotohp one) for the raw CLI.
if [ -z "${GP_AUTH_DATA:-}" ] && [ -n "${GPMC_AUTH_DATA:-}" ]; then
  export GP_AUTH_DATA="$GPMC_AUTH_DATA"
fi
if [ -z "${GP_AUTH_DATA:-}" ] && [ -n "${GOTOHP_AUTH_STRING:-}" ]; then
  export GP_AUTH_DATA="$GOTOHP_AUTH_STRING"
fi

if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime 2>/dev/null || true
  echo "${TZ}" > /etc/timezone 2>/dev/null || true
fi

mkdir -p "$STATE_DIR" "$SIDECAR_DIR" "$WORK_DIR" "$GPMC_CACHE_DIR" 2>/dev/null || true

cmd="${1:-daemon}"
case "$cmd" in
  gpmc)
    shift
    exec gpmc "$@"
    ;;
  run|daemon|doctor|stats|version|--help|-h|--*)
    exec python -m app.main "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
