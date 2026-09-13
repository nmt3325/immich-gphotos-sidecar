#!/usr/bin/env bash
# Entry point: prepares volumes/config, then dispatches to the sidecar CLI.
#   daemon (default) | run | doctor | stats | version
#   gotohp <args...>  -> raw gotohp CLI (e.g. `creds add "<auth string>"`)
#   anything else     -> executed verbatim (sh, bash, python, ...)
set -euo pipefail

STATE_DIR="${STATE_DIR:-/state}"
SIDECAR_DIR="${SIDECAR_DIR:-/sidecar}"
WORK_DIR="${WORK_DIR:-/work}"
GOTOHP_BIN="${GOTOHP_BIN:-/usr/local/bin/gotohp-cli}"
GOTOHP_CONFIG="${GOTOHP_CONFIG:-/config/gotohp.config}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$(dirname "$GOTOHP_CONFIG")}"

if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime 2>/dev/null || true
  echo "${TZ}" > /etc/timezone 2>/dev/null || true
fi

mkdir -p "$STATE_DIR" "$SIDECAR_DIR" "$WORK_DIR" "$(dirname "$GOTOHP_CONFIG")" 2>/dev/null || true
[ -e "$GOTOHP_CONFIG" ] || : > "$GOTOHP_CONFIG" 2>/dev/null || true

# gotohp prefers a `gotohp.config` sitting next to its binary, so point that at
# the persistent /config volume. Keeps credentials across container restarts.
if [ -w "$(dirname "$GOTOHP_BIN")" ]; then
  ln -sf "$GOTOHP_CONFIG" "$(dirname "$GOTOHP_BIN")/gotohp.config" 2>/dev/null || true
fi

cmd="${1:-daemon}"
case "$cmd" in
  gotohp|gotohp-cli)
    shift
    exec "$GOTOHP_BIN" "$@"
    ;;
  run|daemon|doctor|stats|version|--help|-h|--*)
    exec python -m app.main "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
