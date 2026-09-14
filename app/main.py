"""CLI entrypoint: run | daemon | doctor | stats | creds | version."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .backup import BackupRunner, run_doctor, utcnow
from .config import Config
from .gpmc_uploader import GpmcUploader, UploadError, parse_account
from .log import get_logger, setup_logging
from .state import State

VERSION = "1.0.0"
log = get_logger("main")

_STOP = False


def _request_stop(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    log.warning("received signal %s; will stop after the current step", signum)


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):  # pragma: no cover - non main thread
            pass


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str), flush=True)


def _next_delay(cfg: Config, reference: datetime) -> float:
    """Seconds to sleep before the next run."""
    if cfg.schedule_cron:
        try:
            from croniter import croniter

            nxt = croniter(cfg.schedule_cron, reference).get_next(datetime)
            return max(5.0, (nxt - reference).total_seconds())
        except ImportError:
            log.warning("croniter is not installed; falling back to INTERVAL_MINUTES")
        except Exception as exc:  # noqa: BLE001 - bad cron expression
            log.warning("invalid SCHEDULE_CRON %r (%s); using INTERVAL_MINUTES", cfg.schedule_cron, exc)
    return max(60.0, cfg.interval_minutes * 60.0)


def cmd_run(cfg: Config) -> int:
    runner = BackupRunner(cfg)
    try:
        report = runner.run(should_stop=lambda: _STOP)
    finally:
        runner.close()
    _print_json(report)
    return 0 if not report.get("failed") else 2


def cmd_daemon(cfg: Config) -> int:
    log.info(
        "daemon starting (cron=%r interval=%smin run_on_start=%s)",
        cfg.schedule_cron or "-",
        cfg.interval_minutes,
        cfg.run_on_start,
    )
    first = True
    exit_code = 0
    while not _STOP:
        if first and not cfg.run_on_start:
            first = False
        else:
            first = False
            started = utcnow()
            runner = BackupRunner(cfg)
            try:
                report = runner.run(should_stop=lambda: _STOP)
                _print_json(report)
                exit_code = 0 if not report.get("failed") else 2
            except Exception as exc:  # noqa: BLE001 - a daemon must survive one bad run
                log.exception("backup run failed: %s", exc)
                exit_code = 2
            finally:
                runner.close()
            log.info("run finished in %.1fs", (utcnow() - started).total_seconds())
        if _STOP:
            break
        delay = _next_delay(cfg, utcnow())
        log.info("next run in %.0f minutes", delay / 60.0)
        deadline = time.monotonic() + delay
        while not _STOP and time.monotonic() < deadline:
            time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
    log.info("daemon stopped")
    return exit_code


def cmd_doctor(cfg: Config) -> int:
    report = run_doctor(cfg)
    _print_json(report)
    return 0 if report.get("ok") else 1


def cmd_stats(cfg: Config) -> int:
    state = State(cfg.state_path)
    try:
        payload = {"state": state.stats(), "recentRuns": state.last_runs(5)}
    finally:
        state.close()
    _print_json(payload)
    return 0


def _read_token_from_stdin() -> str:
    """Read the oauth_token from stdin so it never lands in the shell history."""
    if sys.stdin is None:
        return ""
    if sys.stdin.isatty():
        print("paste the oauth_token cookie value, then press Enter:", file=sys.stderr, flush=True)
    return (sys.stdin.readline() or "").strip()


def _uploader(cfg: Config, auth_data: Optional[str] = None) -> GpmcUploader:
    return GpmcUploader(
        auth_data=cfg.gpmc_auth_data if auth_data is None else auth_data,
        threads=cfg.gpmc_threads,
        timeout=min(cfg.gpmc_timeout, 120),
        proxy=cfg.gpmc_proxy,
        language=cfg.gpmc_language,
        log_level=cfg.gpmc_effective_log_level,
        cache_dir=cfg.gpmc_cache_dir,
    )


def cmd_creds(cfg: Config, args: argparse.Namespace) -> int:
    """Browser oauth_token -> gpmc auth data, plus inspection helpers."""
    from .google_auth import (
        AuthError,
        create_auth_data,
        mask_auth_data,
        write_auth_data_file,
    )

    action = getattr(args, "creds_command", None) or "show"
    auth_file = Path(cfg.gpmc_auth_data_file)

    if action == "show":
        _print_json(
            {
                "configured": cfg.gpmc_configured,
                "account": parse_account(cfg.gpmc_auth_data) or None,
                "authDataFile": str(auth_file),
                "authDataFileExists": auth_file.exists(),
                "authData": mask_auth_data(cfg.gpmc_auth_data),
            }
        )
        return 0 if cfg.gpmc_configured else 1

    if action == "test":
        if not cfg.gpmc_configured:
            log.error("no auth data configured; run `creds add -` first")
            return 1
        try:
            accounts = _uploader(cfg).ensure_credentials()
        except UploadError as exc:
            log.error("credentials rejected: %s", exc)
            return 1
        _print_json({"ok": True, "accounts": accounts})
        return 0

    token = getattr(args, "token", None) or "-"
    if token == "-":
        token = _read_token_from_stdin()
    proxy = getattr(args, "proxy", None)
    try:
        created = create_auth_data(
            token,
            proxy=cfg.gpmc_proxy if proxy is None else proxy,
            timeout=cfg.gpmc_timeout,
        )
    except AuthError as exc:
        log.error("creds add failed: %s", exc)
        return 1

    auth_data = created["authData"]
    accounts = None
    if getattr(args, "verify", True):
        try:
            accounts = _uploader(cfg, auth_data).ensure_credentials()
        except UploadError as exc:
            log.error("google accepted the token but gpmc cannot use it: %s", exc)
            return 1

    saved = None
    if getattr(args, "save", True):
        try:
            saved = write_auth_data_file(auth_file, auth_data)
        except AuthError as exc:
            log.error("%s", exc)
            return 1

    reveal = getattr(args, "print_auth_data", False) or saved is None
    _print_json(
        {
            "account": created["email"],
            "androidId": created["androidId"],
            "verifiedAccounts": accounts,
            "savedTo": str(saved) if saved else None,
            "authData": auth_data if reveal else mask_auth_data(auth_data),
            "next": (
                f"restart the sidecar; the auth data is read from {saved}"
                if saved
                else "put this value in GPMC_AUTH_DATA and keep it secret"
            ),
        }
    )
    return 0


def _add_common_args(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """Global options, accepted both before and after the sub-command.

    Sub-parser copies default to SUPPRESS so that `--dry-run run` keeps working
    without the sub-parser resetting the value to False.
    """
    none_default = argparse.SUPPRESS if suppress else None
    flag_default = argparse.SUPPRESS if suppress else False
    parser.add_argument("--log-level", dest="log_level", default=none_default, help="override LOG_LEVEL")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=flag_default,
        help="do everything except upload",
    )
    parser.add_argument(
        "--full-scan",
        dest="full_scan",
        action="store_true",
        default=flag_default,
        help="ignore the stored watermark",
    )
    parser.add_argument(
        "--max-assets",
        dest="max_assets",
        type=int,
        default=none_default,
        help="cap the number of assets handled in this run",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="immich-gphotos-sidecar",
        description="Back up Immich content, sidecar metadata and albums to Google Photos via gpmc.",
    )
    _add_common_args(parser)
    common = argparse.ArgumentParser(add_help=False)
    _add_common_args(common, suppress=True)
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("run", "run one backup pass and exit"),
        ("daemon", "run on a schedule (default)"),
        ("doctor", "verify configuration and connectivity"),
        ("stats", "print state database statistics"),
        ("version", "print the sidecar version"),
    ):
        sub.add_parser(name, help=help_text, parents=[common])
    creds = sub.add_parser(
        "creds",
        help="manage the Google Photos credentials (auth data)",
        parents=[common],
        description=(
            "Turn a browser login into gpmc auth data: sign in on "
            "https://accounts.google.com/EmbeddedSetup, copy the oauth_token "
            "cookie and hand it to `creds add`."
        ),
    )
    creds_sub = creds.add_subparsers(dest="creds_command")
    creds_add = creds_sub.add_parser("add", help="exchange an oauth_token for auth data")
    creds_add.add_argument(
        "token",
        nargs="?",
        default="-",
        help="oauth_token cookie value, or - to read it from stdin (default)",
    )
    creds_add.add_argument(
        "--proxy", default=None, help="proxy for the exchange (defaults to GPMC_PROXY)"
    )
    creds_add.add_argument(
        "--no-save",
        dest="save",
        action="store_false",
        help="print the auth data instead of writing GPMC_AUTH_DATA_FILE",
    )
    creds_add.add_argument(
        "--print",
        dest="print_auth_data",
        action="store_true",
        help="also print the full auth data (it contains the master token)",
    )
    creds_add.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="skip the Google Photos round-trip check",
    )
    creds_sub.add_parser("show", help="show which auth data is configured")
    creds_sub.add_parser("test", help="check the configured auth data against Google")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.from_env()
    if args.log_level:
        cfg.log_level = args.log_level.upper()
    if args.dry_run:
        cfg.dry_run = True
    if args.full_scan:
        cfg.full_scan = True
    if args.max_assets is not None:
        cfg.max_assets_per_run = args.max_assets
    setup_logging(cfg.log_level)
    _install_signal_handlers()

    command = args.command or "daemon"
    if command == "version":
        print(VERSION)
        return 0

    if command not in ("doctor", "creds"):
        problems = cfg.problems()
        if problems:
            for problem in problems:
                log.error("config: %s", problem)
            return 1

    if command == "run":
        return cmd_run(cfg)
    if command == "daemon":
        return cmd_daemon(cfg)
    if command == "doctor":
        return cmd_doctor(cfg)
    if command == "stats":
        return cmd_stats(cfg)
    if command == "creds":
        return cmd_creds(cfg, args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
