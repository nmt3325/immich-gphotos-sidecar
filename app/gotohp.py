"""Wrapper around the `gotohp` CLI (https://github.com/xob0t/gotohp).

gotohp talks to the Google Photos *mobile* API, which is the only way to upload
in original quality without consuming API quota. Notes that shape this wrapper:

* The CLI renders a Bubble Tea TUI. gotohp >= v0.10 accepts `--no-tui`, which is
  what this wrapper uses by default (GOTOHP_NO_TUI=true). Older builds can be
  driven on a pseudo terminal instead (GOTOHP_USE_PTY=true); ANSI noise is
  stripped before parsing either way.
* After the TUI finishes, the CLI prints one JSON summary object on stdout:
  {"total":N,"succeeded":N,"failed":N,"results":[{"Path":..,"Success":..,
   "MediaKey":..,"Error":..}],"Album":{"name":..,"itemsAdded":N,"albumKeys":[..]}}
* Uploads are hash deduplicated server side. A duplicate still returns the
  existing MediaKey and is still added to the album given with `-a`, which is
  exactly what album backfill relies on.
"""

from __future__ import annotations

import errno
import json
import os
import pty
import re
import select
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .log import get_logger

log = get_logger("gotohp")

_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"  # other escapes
)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GotohpError(RuntimeError):
    pass


@dataclass
class UploadOutcome:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    album_name: Optional[str] = None
    album_keys: List[str] = field(default_factory=list)
    items_added: int = 0
    media_keys: Dict[str, str] = field(default_factory=dict)  # absolute path -> media key
    errors: Dict[str, str] = field(default_factory=dict)  # absolute path -> error text
    raw: Dict[str, Any] = field(default_factory=dict)


def strip_ansi(text: str) -> str:
    cleaned = _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _CTRL.sub("", cleaned)


def parse_summary(text: str) -> Optional[Dict[str, Any]]:
    """Extract the trailing JSON summary from mixed TUI output."""
    cleaned = strip_ansi(text)
    decoder = json.JSONDecoder()
    index = len(cleaned)
    while True:
        index = cleaned.rfind("{", 0, index)
        if index == -1:
            return None
        try:
            payload, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and (
            "results" in payload or "total" in payload or "Album" in payload
        ):
            return payload


class GotohpClient:
    def __init__(
        self,
        binary: str,
        config_path: str,
        threads: int = 3,
        extra_args: Optional[Sequence[str]] = None,
        use_pty: bool = True,
        timeout: int = 7200,
        disable_filter: bool = False,
        log_level: str = "error",
        no_tui: bool = True,
    ) -> None:
        self.binary = binary
        self.config_path = config_path
        self.threads = max(1, threads)
        self.extra_args = list(extra_args or [])
        self.use_pty = use_pty
        self.timeout = timeout
        self.disable_filter = disable_filter
        self.log_level = log_level
        self.no_tui = no_tui
        self._creds_config_flag: Optional[bool] = None

    # ---- process plumbing ----
    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.setdefault("HOME", "/tmp")
        env["XDG_CONFIG_HOME"] = str(Path(self.config_path).parent)
        env["TERM"] = "xterm-256color" if self.use_pty else "dumb"
        env["NO_COLOR"] = "1"
        return env

    def _run(self, args: Sequence[str], timeout: Optional[int] = None) -> tuple[int, str]:
        argv = [self.binary, *args]
        limit = timeout or self.timeout
        log.debug("running %s", " ".join(argv))
        if self.use_pty:
            try:
                return self._run_pty(argv, limit)
            except OSError as exc:
                log.warning("pty execution failed (%s); falling back to pipes", exc)
        return self._run_pipes(argv, limit)

    def _run_pipes(self, argv: Sequence[str], timeout: int) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise GotohpError(f"gotohp binary not found: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GotohpError(f"gotohp timed out after {timeout}s") from exc
        return proc.returncode, f"{proc.stdout or ''}{proc.stderr or ''}"

    def _run_pty(self, argv: Sequence[str], timeout: int) -> tuple[int, str]:
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is fully controlled
                list(argv),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=self._env(),
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            os.close(master)
            os.close(slave)
            raise GotohpError(f"gotohp binary not found: {self.binary}") from exc
        os.close(slave)
        chunks: List[bytes] = []
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() > deadline:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        proc.kill()
                    raise GotohpError(f"gotohp timed out after {timeout}s")
                readable, _, _ = select.select([master], [], [], 0.5)
                if readable:
                    try:
                        data = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno in (errno.EIO, errno.EBADF):
                            break
                        raise
                    if not data:
                        break
                    chunks.append(data)
                    continue
                if proc.poll() is not None:
                    # drain whatever is still buffered
                    while True:
                        readable, _, _ = select.select([master], [], [], 0.1)
                        if not readable:
                            break
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            data = b""
                        if not data:
                            break
                        chunks.append(data)
                    break
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        returncode = proc.wait()
        return returncode, b"".join(chunks).decode("utf-8", errors="replace")

    # ---- commands ----
    def version(self) -> str:
        code, output = self._run(["version"], timeout=60)
        text = strip_ansi(output).strip()
        if code != 0:
            raise GotohpError(f"`gotohp version` failed (rc={code}): {text[:300]}")
        return text

    def _creds_args(self, *args: str) -> List[str]:
        base = ["creds", *args]
        if self._creds_config_flag is not False:
            return [*base, "-c", self.config_path]
        return base

    def _run_creds(self, *args: str) -> tuple[int, str]:
        code, output = self._run(self._creds_args(*args), timeout=120)
        text = strip_ansi(output)
        if code != 0 and self._creds_config_flag is None and re.search(
            r"unknown (shorthand )?flag|flag provided but not defined|unknown command", text, re.I
        ):
            log.info("`creds` does not accept -c; relying on XDG_CONFIG_HOME instead")
            self._creds_config_flag = False
            code, output = self._run(["creds", *args], timeout=120)
            text = strip_ansi(output)
        elif code == 0 and self._creds_config_flag is None:
            self._creds_config_flag = True
        return code, text

    def creds_list(self) -> List[str]:
        code, text = self._run_creds("list")
        if code != 0 and re.search(r"unknown command", text, re.I):
            code, text = self._run_creds("ls")
        if code != 0:
            log.debug("creds ls rc=%s output=%s", code, text.strip()[:300])
            return []
        accounts: List[str] = []
        for line in text.splitlines():
            line = line.strip().lstrip("*-• ").strip()
            if not line or line.lower().startswith(("no ", "accounts", "usage", "available")):
                continue
            match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
            if match:
                accounts.append(match.group(0))
        return list(dict.fromkeys(accounts))

    def ensure_credentials(self, auth_string: str = "", account: str = "") -> List[str]:
        """Make sure at least one account is stored and selected."""
        accounts = self.creds_list()
        if not accounts and auth_string:
            log.info("registering gotohp credentials from GOTOHP_AUTH_STRING")
            code, text = self._run_creds("add", auth_string)
            if code != 0:
                raise GotohpError(f"`creds add` failed (rc={code}): {text.strip()[:400]}")
            accounts = self.creds_list()
        if not accounts:
            raise GotohpError(
                "no gotohp credentials available - set GOTOHP_AUTH_STRING or run "
                "`docker compose exec <service> gotohp creds add '<auth string>'`"
            )
        if account:
            target = next((item for item in accounts if account.lower() in item.lower()), None)
            if not target:
                raise GotohpError(f"GOTOHP_ACCOUNT={account!r} not found in {accounts}")
            code, text = self._run_creds("set", target)
            if code != 0:
                raise GotohpError(f"`creds set {target}` failed (rc={code}): {text.strip()[:300]}")
        return accounts

    def upload_path(
        self,
        path: Path,
        album: Optional[str] = None,
        recursive: bool = True,
        timeout: Optional[int] = None,
    ) -> UploadOutcome:
        args: List[str] = ["upload", str(path), "-t", str(self.threads), "-l", self.log_level]
        if recursive:
            args.append("-r")
        if self.disable_filter:
            args.append("--disable-filter")
        if self.no_tui:
            args.append("--no-tui")
        if album:
            args += ["-a", album]
        args += ["-c", self.config_path]
        args += self.extra_args
        code, output = self._run(args, timeout=timeout)
        if code != 0 and self.no_tui and re.search(r"unknown flag: --no-tui", output, re.I):
            log.info("this gotohp build does not support --no-tui; retrying with the TUI")
            self.no_tui = False
            args = [arg for arg in args if arg != "--no-tui"]
            code, output = self._run(args, timeout=timeout)
        summary = parse_summary(output)
        if summary is None:
            raise GotohpError(
                f"could not parse gotohp summary (rc={code}); output tail: "
                f"{strip_ansi(output).strip()[-600:]}"
            )
        outcome = UploadOutcome(
            total=int(summary.get("total") or 0),
            succeeded=int(summary.get("succeeded") or 0),
            failed=int(summary.get("failed") or 0),
            raw=summary,
        )
        album_block = summary.get("Album") or summary.get("album") or {}
        if isinstance(album_block, dict):
            outcome.album_name = album_block.get("name")
            outcome.items_added = int(album_block.get("itemsAdded") or 0)
            outcome.album_keys = list(album_block.get("albumKeys") or [])
        for entry in summary.get("results") or []:
            if not isinstance(entry, dict):
                continue
            item_path = entry.get("Path") or entry.get("path") or ""
            media_key = entry.get("MediaKey") or entry.get("mediaKey") or ""
            success = entry.get("Success")
            if success is None:
                success = bool(media_key)
            error = entry.get("Error") or entry.get("error") or ""
            key = os.path.abspath(item_path) if item_path else ""
            if success and media_key and key:
                outcome.media_keys[key] = media_key
            elif key:
                outcome.errors[key] = str(error) or "upload failed"
        if code != 0 and not outcome.media_keys:
            raise GotohpError(
                f"gotohp upload failed (rc={code}): {json.dumps(summary, ensure_ascii=False)[:400]}"
            )
        return outcome
