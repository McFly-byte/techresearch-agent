"""Structured logging setup.

Phase 0 keeps this deliberately small: a single `configure_logging()` that
writes to stdout in a stable format, and a filter that injects the current
request id. We do NOT log settings or secrets — any module that wants to log
configuration must use `Settings.safe_dict()`.
"""

from __future__ import annotations

import logging
import sys

from .correlation import get_request_id

_CONFIGURED = False


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rid = get_request_id()
        record.request_id = rid if rid else "-"
        return True


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] [req=%(request_id)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


__all__ = ["configure_logging"]
