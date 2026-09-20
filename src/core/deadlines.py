"""Strict async deadlines that never wait indefinitely for cancellation."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any, TypeVar, cast

T = TypeVar("T")


def _consume(task: asyncio.Future[Any]) -> None:
    if not task.done():
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.exception()


async def run_with_hard_timeout(
    awaitable: Awaitable[T],
    *,
    timeout: float,
    label: str,
    cancel_grace: float = 2.0,
) -> T:
    """Run ``awaitable`` with a strict wall-clock deadline.

    ``asyncio.wait_for`` waits for a cancellation-resistant child to finish.
    This helper gives cancellation a short grace period and then returns control
    to the caller while consuming the detached task's eventual result.
    """
    body: asyncio.Future[Any] = asyncio.ensure_future(awaitable)
    timer = asyncio.create_task(asyncio.sleep(timeout), name=f"{label}-timer")
    timed_out = False
    try:
        done, _ = await asyncio.wait({body, timer}, return_when=asyncio.FIRST_COMPLETED)
        if body in done:
            return cast(T, await body)
        timed_out = True
        body.cancel()
        done_after_cancel, _ = await asyncio.wait({body}, timeout=max(0.0, cancel_grace))
        if body in done_after_cancel:
            _consume(body)
        else:
            body.add_done_callback(_consume)
        raise TimeoutError(f"{label} after {timeout:g}s")
    finally:
        timer.cancel()
        if not body.done() and not timed_out:
            body.cancel()
            done_after_cancel, _ = await asyncio.wait({body}, timeout=max(0.0, cancel_grace))
            if body not in done_after_cancel:
                body.add_done_callback(_consume)
        await asyncio.gather(timer, return_exceptions=True)


__all__ = ["run_with_hard_timeout"]
