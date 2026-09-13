"""Logging helpers."""

from __future__ import annotations

import logging
import os
import sys

_FMT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FMT, datefmt="%Y-%m-%dT%H:%M:%S%z"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("urllib3").setLevel(max(logging.WARNING, root.level))
    if os.getenv("LOG_HTTP_DEBUG", "").lower() in ("1", "true", "yes"):
        logging.getLogger("urllib3").setLevel(logging.DEBUG)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
