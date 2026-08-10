"""structlog configuration and the redaction processor.

This is a public repository for a custodial service: a leaked API key in a log
line is a withdrawal. Redaction is a pipeline processor rather than a rule
developers have to remember, so a new call site cannot opt out by accident.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from fnmatch import fnmatch
from typing import Any

import structlog

REDACTED = "[redacted]"

#: Matched case-insensitively against every key at every nesting depth.
DENY_PATTERNS: tuple[str, ...] = (
    "*key*",
    "*secret*",
    "*token*",
    "*password*",
    "*signature*",
)

#: Names that match a deny pattern but carry no secret. `key_id` is the
#: non-secret handle we log deliberately; `dedup_key` is a BTCPay delivery id.
ALLOW_KEYS: frozenset[str] = frozenset({"key_id", "dedup_key"})

#: Truncated rather than redacted: an operator needs enough of an address to
#: recognise it in a block explorer, and the full string is a privacy leak in
#: aggregate.
ADDRESS_PATTERNS: tuple[str, ...] = ("address", "*_address", "destination", "checkout_link")

ADDRESS_HEAD = 6
ADDRESS_TAIL = 4

_MAX_DEPTH = 6


def truncate_address(value: str, head: int = ADDRESS_HEAD, tail: int = ADDRESS_TAIL) -> str:
    if len(value) <= head + tail + 3:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def _is_denied(key: str) -> bool:
    lowered = key.lower()
    if lowered in ALLOW_KEYS:
        return False
    return any(fnmatch(lowered, pattern) for pattern in DENY_PATTERNS)


def _is_address(key: str) -> bool:
    lowered = key.lower()
    return any(fnmatch(lowered, pattern) for pattern in ADDRESS_PATTERNS)


def redact_value(key: str, value: Any, depth: int = 0) -> Any:
    if _is_denied(key):
        return REDACTED
    if isinstance(value, str) and _is_address(key):
        return truncate_address(value)
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: redact_value(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        rebuilt = [redact_value(key, item, depth + 1) for item in value]
        return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
    return value


def redaction_processor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {key: redact_value(str(key), value) for key, value in event_dict.items()}


def configure_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redaction_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
