"""Ctrl+C during a threaded upload, download or hashing stops it at once instead of after every job."""
import signal
import threading
import time

import pytest

from fw.pool import run_all


def test_results_keep_the_order_of_the_items():
    assert run_all(lambda x: x * 2, list(range(50)), workers=4) == [x * 2 for x in range(50)]


def test_an_error_cancels_the_jobs_not_started():
    ran = []

    def job(i):
        if i == 0:
            raise ValueError('boom')
        time.sleep(0.05)
        ran.append(i)

    with pytest.raises(ValueError):
        run_all(job, list(range(200)), workers=2)
    time.sleep(0.3)
    assert len(ran) < 20


def test_ctrl_c_reaches_the_main_thread_while_a_slow_job_runs():
    """On Windows a plain wait for a job's result cannot be interrupted: Ctrl+C took effect only when that
    job finished (field report: nothing happened until the whole upload was done). Job 0 here takes 3 s;
    Ctrl+C comes while it runs and must surface at once."""
    def job(i):
        if i == 0:
            time.sleep(3)
        elif i == 1:
            time.sleep(0.1)
            signal.raise_signal(signal.SIGINT)  # what Ctrl+C does: KeyboardInterrupt in the main thread
        else:
            time.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_all(job, list(range(100)), workers=4)
    assert time.monotonic() - started < 1.5


def test_ctrl_c_reaches_the_main_thread_with_a_single_job():
    """Review of PR #6: one job (or workers=1) ran in the main thread, so a blocking call in it (a fetch of a
    single batch waiting up to 120 s for the cache index) still held Ctrl+C back."""
    for items, workers in (([0], 4), ([0, 1, 2], 1)):
        threading.Timer(0.1, signal.raise_signal, (signal.SIGINT,)).start()
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            run_all(lambda i: time.sleep(3), items, workers=workers)
        assert time.monotonic() - started < 1.5


def test_ctrl_c_while_jobs_are_being_submitted_cancels_the_ones_queued(monkeypatch):
    """Review of PR #6: futures was assigned only after every submit, so Ctrl+C in the middle of submitting
    left the jobs already queued running in the background (a Python caller that catches the interrupt
    would see them go on)."""
    import fw.pool as pool_module
    release, started = threading.Event(), []

    class Pool(pool_module.ThreadPoolExecutor):
        submitted = 0

        def submit(self, fn, *args):
            Pool.submitted += 1
            if Pool.submitted == 6:
                raise KeyboardInterrupt
            return super().submit(fn, *args)

    def job(i):
        started.append(i)
        release.wait(5)

    monkeypatch.setattr(pool_module, 'ThreadPoolExecutor', Pool)
    with pytest.raises(KeyboardInterrupt):
        run_all(job, list(range(10)), workers=2)
    release.set()
    time.sleep(0.3)
    assert sorted(started) == [0, 1]
