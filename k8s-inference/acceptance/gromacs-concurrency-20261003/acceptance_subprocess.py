"""Bounded local observation commands; never terminate Kubernetes workloads."""

import asyncio
import os
import signal
from subprocess import CompletedProcess


async def read_command_result(arguments, *, timeout=30):
    """Retain both streams/status without abandoning the pipe-draining task.

    Cancelling communicate() on timeout, then waiting for the killed child,
    can deadlock while its buffered stdout is paused. Shield the reader and
    drain it after terminating this command's isolated process group (including
    credential-plugin children). Only that local command group is signalled.
    """
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(communication), timeout=5)
        except TimeoutError:
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
        raise
    return CompletedProcess(arguments, process.returncode, stdout, stderr)


async def read_command(arguments, *, timeout=30):
    """Compatibility reader: successful stdout bytes or an output-free error."""
    result = await read_command_result(arguments, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Local read/observation command failed")
    return result.stdout
