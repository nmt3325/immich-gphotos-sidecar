#!/usr/bin/env python3
"""Fake gotohp CLI: mimics the TUI noise + trailing JSON summary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

STORE = Path(os.environ.get("FAKE_GOTOHP_STORE", "/tmp/fake-gotohp-store.json"))
EXTS = {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".webp", ".gif", ".bin"}


def load_store() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except json.JSONDecodeError:
            pass
    return {"media": {}, "albums": {}, "calls": []}


def save(store: dict) -> None:
    STORE.write_text(json.dumps(store, ensure_ascii=False, indent=2))


def media_key(path: Path) -> str:
    digest = hashlib.sha1(path.read_bytes() + path.name.encode()).hexdigest()
    return "AF1Qip" + digest[:26]


def tui_noise() -> None:
    # emulate bubbletea redraws, including a stray brace to stress the parser
    sys.stdout.write("\x1b[?25l\x1b[2K\rUploading... { spinner }\n")
    sys.stdout.write("\x1b[2K\r\x1b[32m\u2714\x1b[0m done\n\x1b[?25h")


def main() -> int:
    args = sys.argv[1:]
    store = load_store()
    store["calls"].append(args)
    if not args:
        print("usage: gotohp <command>")
        save(store)
        return 1
    command = args[0]

    if command == "version":
        print("gotohp fake 9.9.9 (test double)")
        save(store)
        return 0

    if command == "creds":
        if os.environ.get("FAKE_NO_CREDS_CONFIG") and ("-c" in args or "--config" in args):
            print("unknown shorthand flag: 'c' in -c", file=sys.stderr)
            save(store)
            return 1
        sub = args[1] if len(args) > 1 else "ls"
        accounts = store.setdefault("accounts", [])
        if sub in ("ls", "list"):
            if not accounts:
                print("no accounts stored")
                save(store)
                return 0
            for index, account in enumerate(accounts):
                print(f"{'*' if index == 0 else '-'} {account}")
            save(store)
            return 0
        if sub == "add":
            accounts.append(os.environ.get("FAKE_ACCOUNT_EMAIL", "tester@example.com"))
            print("credentials added")
            save(store)
            return 0
        if sub in ("set", "select"):
            wanted = args[2] if len(args) > 2 else ""
            if wanted in accounts:
                accounts.remove(wanted)
                accounts.insert(0, wanted)
            print(f"active account: {accounts[0] if accounts else '-'}")
            save(store)
            return 0
        print(f"unknown creds subcommand {sub}", file=sys.stderr)
        save(store)
        return 1

    if command == "upload":
        target = Path(args[1])
        album = None
        if "-a" in args:
            album = args[args.index("-a") + 1]
        elif "--album" in args:
            album = args[args.index("--album") + 1]
        files = [
            path
            for path in sorted(target.rglob("*"))
            if path.is_file() and path.suffix.lower() in EXTS
        ]
        tui_noise()
        results = []
        album_keys = []
        added = 0
        for path in files:
            key = media_key(path)
            duplicate = key in store["media"]
            store["media"][key] = {"name": path.name, "album": album, "duplicate": duplicate}
            results.append(
                {"Path": str(path), "Success": True, "MediaKey": key, "Error": ""}
            )
            if album:
                members = store["albums"].setdefault(album, [])
                if key not in members:
                    members.append(key)
                    added += 1
        if album:
            album_keys = ["AF1Qip" + hashlib.sha1(album.encode()).hexdigest()[:26]]
        summary = {
            "total": len(files),
            "succeeded": len(files),
            "failed": 0,
            "results": results,
        }
        if album:
            summary["Album"] = {"name": album, "itemsAdded": added, "albumKeys": album_keys}
        save(store)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    print(f"unknown command {command}", file=sys.stderr)
    save(store)
    return 1


if __name__ == "__main__":
    sys.exit(main())
