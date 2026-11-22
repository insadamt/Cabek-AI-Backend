"""Expose queued and running background work for updater admission draining.

Depends on ThreadPoolExecutor callbacks. A task remains active until all of its
handoff work has returned, including scheduling the analysis after capture.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import RLock


class TrackedExecutor(ThreadPoolExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tracking_lock = RLock()
        self.tracked = set()

    def submit(self, *args, **kwargs):
        with self.tracking_lock:
            future = super().submit(*args, **kwargs)
            self.tracked.add(future)
            future.add_done_callback(self.forget)
            return future

    def forget(self, future):
        with self.tracking_lock:
            self.tracked.discard(future)

    @property
    def active_count(self):
        with self.tracking_lock:
            return len(self.tracked)
