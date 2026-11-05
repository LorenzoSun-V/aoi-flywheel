"""The local cache (fw.cache)."""
import threading
import time

from fw.cache import Cache


def test_close_does_not_wait_for_a_thread_still_using_the_index(tmp_path):
    """Review of PR #6: after Ctrl+C, `fw get` closed the cache while a download thread held its lock
    waiting for SQLite (up to 120 s), so the command hung before it could exit."""
    cache = Cache(tmp_path)
    holding, release = threading.Event(), threading.Event()

    def worker():
        with cache._lock:
            holding.set()
            release.wait(3)

    t = threading.Thread(target=worker)
    t.start()
    holding.wait(1)
    started = time.monotonic()
    cache.close()
    assert time.monotonic() - started < 0.5
    release.set()
    t.join()
