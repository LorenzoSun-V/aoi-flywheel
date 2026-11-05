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
