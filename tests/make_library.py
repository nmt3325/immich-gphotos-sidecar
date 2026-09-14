"""Materialise a fake Immich upload location from the mock server's data.

The mock reports paths like `upload/upload/owner-1/aa/bb/<assetId>.jpg`, which
is how Immich stores them relative to /usr/src/app: with the default storage
template the file on disk is named after the asset id, not after the original
file name. A real deployment mounts
UPLOAD_LOCATION (the directory the leading `upload/` component refers to), so
the first component is stripped by default.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_immich import ASSETS, JPEG  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1])
    snapshot_path = Path(sys.argv[2])
    strip = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    snapshot = {}
    for asset in ASSETS.values():
        parts = [part for part in asset["originalPath"].split("/") if part not in ("", ".")]
        target = root.joinpath(*parts[strip:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(JPEG)
        snapshot[str(target.relative_to(root))] = hashlib.sha256(JPEG).hexdigest()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    print(f"library ready: {len(snapshot)} file(s) under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
