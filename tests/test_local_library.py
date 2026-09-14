"""Unit tests for the local library reader (no network, no Immich)."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.local_library import (  # noqa: E402
    LocalLibrary,
    LocalLibraryError,
    file_checksum,
)

PAYLOAD = b"immich-original-bytes\n" * 8
OTHER = b"a different photo\n" * 4


def _asset(original_path: str, blob: bytes = PAYLOAD, **extra):
    asset = {
        "id": "aaaa1111-0000-4000-8000-000000000001",
        "originalPath": original_path,
        "originalFileName": PurePath_name(original_path),
        "checksum": base64.b64encode(hashlib.sha1(blob).digest()).decode("ascii"),
        "exifInfo": {"fileSizeInByte": len(blob)},
    }
    asset.update(extra)
    return asset


def PurePath_name(path: str) -> str:
    return path.rstrip("/").split("/")[-1]


def _write(root: Path, relative: str, blob: bytes = PAYLOAD) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    return target


def test_relative_original_path_maps_onto_the_mount():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "immich-library"
        expected = _write(root, "library/admin/2024/IMG_0001.jpg")
        library = LocalLibrary([str(root)])
        found = library.locate(_asset("upload/library/admin/2024/IMG_0001.jpg"))
        assert found == expected, found
        assert library.resolved == 1 and library.missed == 0


def test_absolute_container_path_maps_onto_the_mount():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "data"
        expected = _write(root, "library/admin/2024/IMG_0002.jpg")
        library = LocalLibrary([str(root)])
        found = library.resolve("/usr/src/app/upload/library/admin/2024/IMG_0002.jpg")
        assert found == expected, found


def test_mount_that_matches_the_full_path_also_works():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "mnt"
        expected = _write(root, "usr/src/app/upload/library/a/IMG.jpg")
        library = LocalLibrary([str(root)])
        assert library.resolve("/usr/src/app/upload/library/a/IMG.jpg") == expected


def test_custom_prefix_is_stripped():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "photos"
        expected = _write(root, "2024/IMG_0003.jpg")
        library = LocalLibrary([str(root)], prefixes=["/mnt/tank/immich/library/admin"])
        found = library.resolve("/mnt/tank/immich/library/admin/2024/IMG_0003.jpg")
        assert found == expected, found


def test_missing_file_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        library = LocalLibrary([str(Path(tmp) / "root")])
        assert library.locate(_asset("upload/library/admin/nope.jpg")) is None
        assert library.missed == 1


def test_paths_outside_the_root_are_refused():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        secret = Path(tmp) / "secret.txt"
        secret.write_bytes(PAYLOAD)
        library = LocalLibrary([str(root)])
        assert library.resolve("../secret.txt") is None


def test_symlinks_out_of_the_library_are_refused():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        outside = Path(tmp) / "outside.jpg"
        outside.write_bytes(PAYLOAD)
        os.symlink(outside, root / "linked.jpg")
        library = LocalLibrary([str(root)])
        assert library.resolve("upload/linked.jpg") is None


def test_checksum_mismatch_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        _write(root, "library/IMG_0004.jpg", OTHER)
        library = LocalLibrary([str(root)])
        asset = _asset("upload/library/IMG_0004.jpg")
        asset["exifInfo"]["fileSizeInByte"] = len(OTHER)
        try:
            library.locate(asset)
        except LocalLibraryError as exc:
            assert "checksum mismatch" in str(exc), exc
        else:
            raise AssertionError("a wrong file was accepted")


def test_size_mismatch_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        _write(root, "library/IMG_0005.jpg", OTHER)
        library = LocalLibrary([str(root)])
        asset = _asset("upload/library/IMG_0005.jpg")  # advertises len(PAYLOAD)
        try:
            library.locate(asset)
        except LocalLibraryError as exc:
            assert "immich reported" in str(exc), exc
        else:
            raise AssertionError("a truncated file was accepted")


def test_checksum_check_can_be_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        target = _write(root, "library/IMG_0006.jpg", OTHER)
        library = LocalLibrary([str(root)], verify_checksum=False)
        asset = _asset("upload/library/IMG_0006.jpg")
        asset["exifInfo"]["fileSizeInByte"] = len(OTHER)
        assert library.locate(asset) == target


def test_locate_needs_an_original_path():
    with tempfile.TemporaryDirectory() as tmp:
        library = LocalLibrary([tmp])
        assert library.locate({"id": "x"}) is None


def test_disabled_without_roots():
    library = LocalLibrary([])
    assert not library.enabled
    assert library.locate(_asset("upload/library/IMG.jpg")) is None
    assert library.describe()["enabled"] is False


def test_describe_reports_missing_roots():
    with tempfile.TemporaryDirectory() as tmp:
        library = LocalLibrary([str(Path(tmp) / "not-mounted")])
        assert library.describe()["missingRoots"], library.describe()


def test_materialize_copies_into_the_work_dir():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        source = _write(root, "library/IMG_0007.jpg")
        target = Path(tmp) / "work" / "cache" / "IMG_0007.jpg"
        library = LocalLibrary([str(root)])
        size = library.materialize(source, target)
        assert size == len(PAYLOAD), size
        assert target.read_bytes() == PAYLOAD
        assert not target.with_name(target.name + ".part").exists()
        assert file_checksum(target) == file_checksum(source)


def test_materialize_works_on_a_read_only_library():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        source = _write(root, "library/IMG_0008.jpg")
        before = source.stat()
        target = Path(tmp) / "work" / "IMG_0008.jpg"
        os.chmod(source.parent, 0o555)
        try:
            LocalLibrary([str(root)]).materialize(source, target)
        finally:
            os.chmod(source.parent, 0o755)
        after = source.stat()
        assert target.read_bytes() == PAYLOAD
        assert (before.st_size, int(before.st_mtime)) == (after.st_size, int(after.st_mtime))
        assert sorted(p.name for p in source.parent.iterdir()) == ["IMG_0008.jpg"]


def test_second_asset_reuses_the_learned_mapping():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        first = _write(root, "library/admin/IMG_0009.jpg")
        second = _write(root, "library/admin/IMG_0010.jpg", OTHER)
        library = LocalLibrary([str(root)])
        assert library.resolve("upload/library/admin/IMG_0009.jpg") == first
        hint = library._hint
        assert library.resolve("upload/library/admin/IMG_0010.jpg") == second
        assert library._hint == hint, (library._hint, hint)


def main() -> int:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - this is the test reporter
            failures += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[PASS] {name}")
    print()
    print(f"{len(tests) - failures}/{len(tests)} local library tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
