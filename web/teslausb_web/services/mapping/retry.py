from __future__ import annotations

import logging
import sqlite3
import time
from typing import TYPE_CHECKING, ParamSpec, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

_TRANSIENT_SUBSTRINGS = (
    "database is locked",
    "disk i/o error",
    "unable to open database file",
)


def _is_transient_db_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return any(fragment in message for fragment in _TRANSIENT_SUBSTRINGS)


def _with_db_retry(
    fn: Callable[P, R],
    *,
    delay_seconds: float = 0.2,
) -> Callable[P, R]:
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if not _is_transient_db_error(exc):
                raise
            logger.warning("Transient DB error in %s (%s); retrying once", fn.__name__, exc)
            time.sleep(delay_seconds)
            return fn(*args, **kwargs)

    return wrapper
