"""Structured logging helpers."""

from __future__ import annotations

import logging
import sys
from typing import Any


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once for the process."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def redact_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with sensitive keys masked."""
    sensitive = {"api_key", "authorization", "token", "password", "secret"}
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key.lower() in sensitive or any(s in key.lower() for s in sensitive):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted
