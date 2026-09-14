#!/usr/bin/env bash
# Offline end-to-end test: mock Immich + fake gpmc. Three runs download the
# originals over HTTP, two more read them straight from a fake local library.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${PY:-/tmp/venv/bin/python}"
BASE=/tmp/e2e
PORT=8099
STORE=/tmp/fake-gpmc-store.json

rm -rf "$BASE" "$STORE"
mkdir -p "$BASE"/{state,sidecar,work,config,home}

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
# the fake gpmc package shadows the real library
export PYTHONPATH="$ROOT/tests/fake_gpmc${PYTHONPATH:+:$PYTHONPATH}"
export FAKE_GPMC_STORE="$STORE"
export HOME="$BASE/home"
export GPMC_AUTH_DATA='androidId=fakeandroidid&Email=tester%40example.com&Token=fake-token&lang=en_US'
export GPMC_THREADS=2
export GPMC_CACHE_DIR="$BASE/config"
export ALBUM_BACKEND=gpmc
export METADATA_BACKEND=embed
export UPLOAD_BATCH_SIZE=200
export LOG_LEVEL=INFO

echo '############ unit tests ############'
"$PY" tests/test_google_auth.py 2>&1 | tail -25
"$PY" tests/test_local_library.py 2>&1 | tail -25

echo '############ run 1: full scan ############'
"$PY" -m app.main run --full-scan 2>&1 | tail -30

echo '############ run 2: incremental no-op ############'
"$PY" -m app.main run 2>&1 | tail -20

echo '############ run 3: album backfill (existing google album reused) ############'
"$PY" - <<'PY_WIPE'
import os, sqlite3
path = os.path.join(os.environ["STATE_DIR"], "sidecar-state.sqlite3")
conn = sqlite3.connect(path)
conn.execute("DELETE FROM album_links")
conn.commit()
conn.close()
print("album_links wiped to force backfill")
PY_WIPE
"$PY" -m app.main run 2>&1 | tail -20

echo '############ doctor ############'
"$PY" -m app.main doctor 2>&1 | tail -40 || true

echo '############ assertions ############'
"$PY" tests/assert_e2e.py "$STATE_DIR" "$SIDECAR_DIR" "$FAKE_GPMC_STORE"

echo '############ run 4: local library (embed -> copy into /work) ############'
LIB="$BASE/library"
SNAP="$BASE/library.sha256.json"
"$PY" tests/make_library.py "$LIB" "$SNAP"
export IMMICH_LIBRARY_PATH="$LIB"
export ASSET_SOURCE=local
export STATE_DIR="$BASE/state-local"
export SIDECAR_DIR="$BASE/sidecar-local"
export WORK_DIR="$BASE/work-local"
export FAKE_GPMC_STORE="$BASE/fake-gpmc-local.json"
mkdir -p "$STATE_DIR" "$SIDECAR_DIR" "$WORK_DIR"
"$PY" -m app.main run --full-scan 2>&1 | tail -30
"$PY" tests/assert_local_e2e.py "$STATE_DIR" "$LIB" "$SNAP" 0

echo '############ run 5: local library, zero copy (METADATA_BACKEND=none) ############'
export METADATA_BACKEND=none
export STATE_DIR="$BASE/state-direct"
export SIDECAR_DIR="$BASE/sidecar-direct"
export WORK_DIR="$BASE/work-direct"
export FAKE_GPMC_STORE="$BASE/fake-gpmc-direct.json"
mkdir -p "$STATE_DIR" "$SIDECAR_DIR" "$WORK_DIR"
"$PY" -m app.main run --full-scan 2>&1 | tail -30
"$PY" tests/assert_local_e2e.py "$STATE_DIR" "$LIB" "$SNAP" 3

echo '############ doctor (local library) ############'
"$PY" -m app.main doctor 2>&1 | tail -45 || true
