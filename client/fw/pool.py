"""Running jobs in threads so that Ctrl+C stops them at once.

`with ThreadPoolExecutor() as pool: pool.map(...)` cannot be interrupted: on Ctrl+C the `with` block still
waits for every queued job, so an upload went on until the last file was sent (and over a dropped network
it kept retrying). Here the main thread waits in short steps, so the interrupt reaches it, and then every
job that has not started is cancelled; only the ones already running finish (or time out).
"""
from __future__ import annotations

import threading
from concurrent.futures import FIRST_EXCEPTION, CancelledError, ThreadPoolExecutor, wait
from typing import Callable, List, Sequence, TypeVar

T = TypeVar('T')
R = TypeVar('R')
STEP = 0.2  # seconds the main thread waits at a time; a blocking wait would not see Ctrl+C on Windows


def run_all(fn: Callable[[T], R], items: Sequence[T], workers: int) -> List[R]:
    """fn(item) for every item, in up to `workers` threads; results in the order of items. The first
    error (or Ctrl+C) cancels every job not started yet and is raised."""
    if not items:
        return []
    # Even one job, or workers=1, runs in a thread: in the main thread a blocking call inside fn (a request,
    # a wait for the cache index) would hold Ctrl+C back just the same.
    stop = threading.Event()

    def job(item):
        # Set on error or Ctrl+C. Cancelling the futures is not enough: Ctrl+C can come after submit() has
        # queued a job and before its future is recorded, and that job must not start either.
        if stop.is_set():
            raise CancelledError()
        return fn(item)

    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(items))))
    futures = []
    try:
        for item in items:
            futures.append(pool.submit(job, item))
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=STEP, return_when=FIRST_EXCEPTION)
            for f in done:
                error = f.exception()
                if error is not None:
                    raise error
        return [f.result() for f in futures]
    except BaseException:
        stop.set()
        for f in futures:
            f.cancel()
        raise
    finally:
        pool.shutdown(wait=False)
