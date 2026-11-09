"""Running it for more than one person: how many recognitions run at once, how often each account may do the
expensive things, and numbers that say how the service is doing. All in memory; one server process.
"""
from collections import deque
import os
import threading
import time


def _limit(name, default):
    """A limit as (count, seconds), from VOICE_RATE_<NAME>="count/seconds" when set."""
    raw = os.getenv(f"VOICE_RATE_{name.upper()}", "")
    try:
        count, seconds = (int(part) for part in raw.split("/"))
        if count > 0 and seconds > 0:
            return count, seconds
    except ValueError:
        pass
    return default


LIMITS = {
    "auth": _limit("auth", (20, 60)),          # sign-in and sign-up attempts per IP address
    "turn": _limit("turn", (30, 60)),          # questions to the assistant per account (typed or spoken)
    "upload": _limit("upload", (30, 3600)),    # files: knowledge, meetings, voices
    "lookup": _limit("lookup", (60, 60)),      # translations, explanations, readings
    "task": _limit("task", (20, 3600)),        # background tasks and quizzes
}
LIMIT_MESSAGES = {"auth": "尝试太频繁", "turn": "说得太快了", "upload": "上传太频繁", "lookup": "查询太频繁", "task": "提交太频繁"}


class RateLimiter:
    """Sliding window per key: at most `count` events in any `seconds`."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.events, self.lock = clock, {}, threading.Lock()

    def hit(self, kind, key):
        """None when allowed (and counted), otherwise seconds until the next one would be."""
        count, seconds = LIMITS[kind]
        now = self.clock()
        with self.lock:
            window = self.events.setdefault((kind, key), deque())
            while window and window[0] <= now - seconds:
                window.popleft()
            if len(window) >= count:
                return max(1, int(window[0] + seconds - now + .999))
            window.append(now)
            if len(self.events) > 50000:  # forget idle keys rather than grow forever
                for stale in [k for k, w in self.events.items() if not w or w[-1] <= now - 3600]:
                    del self.events[stale]
        return None

    def message(self, kind, retry):
        return f"{LIMIT_MESSAGES[kind]}，请 {retry} 秒后再试"


class Metrics:
    """Counters and recent latencies (last 500 of each), for the admin's status card."""

    def __init__(self, keep=500):
        self.keep, self.counts, self.samples, self.lock, self.started = keep, {}, {}, threading.Lock(), time.time()

    def count(self, name, amount=1):
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + amount

    def observe(self, name, seconds):
        with self.lock:
            self.samples.setdefault(name, deque(maxlen=self.keep)).append(seconds)

    @staticmethod
    def _quantile(values, q):
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    def snapshot(self):
        with self.lock:
            latencies = {name: {"n": len(values), "p50": round(self._quantile(values, .5), 3), "p95": round(self._quantile(values, .95), 3),
                                "max": round(max(values), 3)} for name, values in self.samples.items() if values}
            return {"uptime_seconds": round(time.time() - self.started), "counts": dict(sorted(self.counts.items())), "latency": latencies}


class RecognitionGate:
    """At most `size` recognitions at once; the rest wait their turn. Knows how many are running and waiting, so
    a page can be told it is queued instead of seeing nothing."""

    def __init__(self, size):
        self.size, self.semaphore, self.lock = size, threading.BoundedSemaphore(size), threading.Lock()
        self.active = self.waiting = 0

    def busy(self):
        with self.lock:
            return self.active >= self.size

    def queue_length(self):
        with self.lock:
            return self.waiting

    def __enter__(self):
        with self.lock:
            self.waiting += 1
        started = time.monotonic()
        self.semaphore.acquire()
        with self.lock:
            self.waiting -= 1
            self.active += 1
        self.waited = time.monotonic() - started
        return self

    def __exit__(self, *exc):
        with self.lock:
            self.active -= 1
        self.semaphore.release()
        return False


def recognition_slots():
    """How many recognitions may run at once: VOICE_ASR_CONCURRENCY, default 2 (each uses two CPU threads)."""
    try:
        return max(1, int(os.getenv("VOICE_ASR_CONCURRENCY", "2")))
    except ValueError:
        return 2
