#!/usr/bin/env bash
# Offline end-to-end test: mock Immich + fake gotohp, three consecutive runs.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${PY:-/tmp/venv/bin/python}"
BASE=/tmp/e2e
PORT=8099

rm -rf "$BASE" /tmp/fake-gotohp-store.json
mkdir -p "$BASE"/{state,sidecar,work,config}

cleanup() {
  if [ -f "$BASE/mock.pid" ]; then kill "$(cat "$BASE/mock.pid")" 2>/dev/null || true; fi
}
trap cleanup EXIT

python3 tests/mock_immich.py --port "$PORT" > "$BASE/mock.log" 2>&1 &
echo $! > "$BASE/mock.pid"

for _ in $(seq 1 40); do
  if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)"; then break; fi
  sleep 0.25
done

export IMMICH_BASE_URL="http://127.0.0.1:$PORT"
export IMMICH_API_KEY=test-key
export STATE_DIR="$BASE/state"
export SIDECAR_DIR="$BASE/sidecar"
export WORK_DIR="$BASE/work"
export GOTOHP_BIN="$ROOT/tests/fake_gotohp.py"
export GOTOHP_CONFIG="$BASE/config/gotohp.config"
export GOTOHP_AUTH_STRING='fake-auth-string'
export GOTOHP_ACCOUNT='tester@example.com'
export GOTOHP_THREADS=2
export GOTOHP_USE_PTY=false
export ALBUM_BACKEND=gotohp
export METADATA_BACKEND=embed
export UPLOAD_BATCH_SIZE=200
export LOG_LEVEL=INFO
export FAKE_GOTOHP_STORE=/tmp/fake-gotohp-store.json

echo '############ run 1: full scan ############'
"$PY" -m app.main run --full-scan 2>&1 | tail -30

echo '############ run 2: incremental no-op ############'
"$PY" -m app.main run 2>&1 | tail -20

echo '############ run 3: album backfill under a pty ############'
"$PY" - <<'PY_WIPE'
import os, sqlite3
path = os.path.join(os.environ["STATE_DIR"], "sidecar-state.sqlite3")
conn = sqlite3.connect(path)
conn.execute("DELETE FROM album_links")
conn.commit()
conn.close()
print("album_links wiped to force backfill")
PY_WIPE
GOTOHP_USE_PTY=true "$PY" -m app.main run 2>&1 | tail -20

echo '############ doctor ############'
"$PY" -m app.main doctor 2>&1 | tail -40 || true

echo '############ assertions ############'
"$PY" tests/assert_e2e.py "$STATE_DIR" "$SIDECAR_DIR" "$FAKE_GOTOHP_STORE"
