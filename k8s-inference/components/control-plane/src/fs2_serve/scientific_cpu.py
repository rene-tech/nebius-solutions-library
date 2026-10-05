"""Bound expensive, pure scientific metadata work without blocking API probes.

Only synchronous computation belongs here: database/network clients, admission
decisions and mutable ownership must remain in their existing async scope. The
dedicated executor bounds active work across event loops in this process. A
cancelled caller cannot leave a transaction or lock while its running callback
is still computing a value for that scope.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fs2-scientific-cpu")


async def run_scientific_cpu(function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Compute off-loop; propagate cancellation only after running work drains."""
    work = _EXECUTOR.submit(contextvars.copy_context().run, partial(function, *args, **kwargs))
    completed = asyncio.wrap_future(work)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(completed)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            if work.cancel():
                # Queued work has no side effects and need not start at all.
                completed.cancel()
                raise cancellation from None
            if completed.done():
                # Consume any eventual exception without replacing the caller's
                # cancellation or emitting an unobserved-future warning.
                if not completed.cancelled():
                    completed.exception()
                raise cancellation from None
            # Raw Task.cancel(), including repeated cancellation, does not
            # abandon an already-running callback or release its owner's lock.
            continue
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
        if cancellation is not None:
            raise cancellation
        return result
