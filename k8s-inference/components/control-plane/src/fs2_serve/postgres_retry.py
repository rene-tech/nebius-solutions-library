"""Bounded retries for idempotent PostgreSQL transactions."""

from __future__ import annotations

import asyncio
import functools
from typing import Any

import asyncpg


def retry_serialization(method: Any) -> Any:
    """Retry a whole idempotent transaction on deadlock/serialization abort."""

    @functools.wraps(method)
    async def wrapped(self: object, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                return await method(self, *args, **kwargs)
            except asyncpg.PostgresError as exc:
                if getattr(exc, "sqlstate", None) not in {"40P01", "40001"} or attempt == 2:
                    raise
                await asyncio.sleep(0.01 * (2**attempt))
        raise AssertionError("unreachable")

    return wrapped
