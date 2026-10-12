"""Bounded retries for immutable object I/O, never for execution submission."""

import socket
import time

import httpx
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError, ReadTimeoutError

TRANSIENT = (
    httpx.TransportError,
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)


def retry_transfer(action, *, attempts=6):
    """Reopen/rewind inside action; authorization and content errors are final.

    Each attempt obtains a fresh scoped grant. There is no retry of MD work or
    pointer publication here. A completed PUT is safe to replay because its key
    is the hash of exactly the same bytes.
    """
    for attempt in range(attempts):
        try:
            return action()
        except TRANSIENT:
            if attempt + 1 == attempts:
                raise
            # Brief DNS/connection outages must not turn completed GPU work
            # into another execution. Still bounded: 11.5 seconds of backoff.
            time.sleep(min(0.5 * 2**attempt, 4))
