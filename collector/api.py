"""Thread-pooled, rate-limited client for the official Brawl Stars API via the
RoyaleAPI proxy. Concurrency + a token-bucket limiter + 429 backoff keep us
polite so the shared proxy never bans the key.

The rate that matters is Supercell's per-key limit on OUR key (the proxy is a
transparent passthrough, not a metered service). The API reports that limit in
the x-ratelimit-limit response header - we log it so we can size the crawl to
the real ceiling instead of guessing (Fable research Q2).
"""
import threading
import time
import requests
from requests.adapters import HTTPAdapter

from . import config


class RateLimiter:
    """Token bucket: at most TARGET_RPS requests/second, shared across threads.

    CRITICAL (Fable Q2): reserve the slot UNDER the lock, then sleep OUTSIDE it.
    The old version slept while holding the lock, serializing every worker and
    capping real throughput far below the target even with 24 threads."""
    def __init__(self, rps: float):
        self.base_interval = 1.0 / max(rps, 0.1)
        self.min_interval = self.base_interval
        self.max_interval = 1.0  # never crawl slower than ~1 rps, even after 429s
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_time)   # my reserved instant
            self.next_time = slot + self.min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)                 # sleep WITHOUT the lock held

    def slow_down(self, factor: float = 1.3):
        # Bounded so a burst of early 429s can't collapse the whole 5.5h run to
        # a crawl; recovers toward base_interval via speed_up on clean responses.
        with self.lock:
            self.min_interval = min(self.min_interval * factor, self.max_interval)

    def speed_up(self):
        # Gentle recovery after a clean stretch, back toward the target rate.
        with self.lock:
            if self.min_interval > self.base_interval:
                self.min_interval = max(self.base_interval, self.min_interval * 0.99)


class Api:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {config.API_KEY}"})
        # Connection pool must be >= concurrency or requests silently serializes
        # excess workers onto a pool of 10 (Fable Q2 - second throughput bug).
        pool = config.MAX_CONCURRENCY + 8
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.limiter = RateLimiter(config.TARGET_RPS)
        self.calls = 0
        # Health counters (surfaced per-run so we can SEE whether pushing the
        # rate harder actually works or just trips 429s / bans).
        self.rate_limited = 0     # 429s (proxy throttling us)
        self.errors = 0           # network exceptions that exhausted retries
        self.forbidden = 0        # 403s (bad / unwhitelisted key) - key problem
        self.server_errors = 0    # 5xx from upstream
        self.limit_header = 0     # last seen x-ratelimit-limit (our real ceiling)
        self.remaining_min = None # lowest x-ratelimit-remaining seen (headroom)
        self.latency_sum = 0.0    # sum of request round-trip seconds (200s only)
        self.latency_n = 0        # count, for avg latency (the throughput lever)
        self._lock = threading.Lock()

    def health(self) -> dict:
        avg_latency_ms = round(1000 * self.latency_sum / self.latency_n) if self.latency_n else 0
        return {
            "calls": self.calls,
            "rateLimited": self.rate_limited,
            "errors": self.errors,
            "forbidden": self.forbidden,
            "serverErrors": self.server_errors,
            "limitHeader": self.limit_header,
            "remainingMin": self.remaining_min if self.remaining_min is not None else -1,
            "avgLatencyMs": avg_latency_ms,
        }

    def _note_ratelimit_headers(self, r):
        # The proxy/Supercell report the real per-key ceiling; record it so the
        # next run can be sized to actual headroom instead of folklore.
        try:
            lim = r.headers.get("x-ratelimit-limit")
            rem = r.headers.get("x-ratelimit-remaining")
            with self._lock:
                if lim is not None:
                    self.limit_header = int(str(lim).split(",")[0].strip())
                if rem is not None:
                    val = int(str(rem).split(",")[0].strip())
                    if self.remaining_min is None or val < self.remaining_min:
                        self.remaining_min = val
        except (ValueError, TypeError):
            pass

    def _get(self, path: str):
        """GET with rate limiting + 429/5xx backoff. Returns parsed JSON or None."""
        url = f"{config.PROXY_BASE}/{path}"
        for attempt in range(4):
            self.limiter.acquire()
            with self._lock:
                self.calls += 1
            try:
                t0 = time.monotonic()
                r = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
                dt = time.monotonic() - t0
            except requests.RequestException:
                if attempt == 3:
                    with self._lock:
                        self.errors += 1
                time.sleep(0.5 * (attempt + 1))
                continue
            self._note_ratelimit_headers(r)
            if r.status_code == 200:
                with self._lock:
                    self.latency_sum += dt
                    self.latency_n += 1
                self.limiter.speed_up()
                try:
                    return r.json()
                except ValueError:
                    return None
            if r.status_code == 429:
                with self._lock:
                    self.rate_limited += 1
                self.limiter.slow_down()
                # Honor Retry-After if the server tells us how long to wait.
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 1.5 * (attempt + 1)
                except (ValueError, TypeError):
                    wait = 1.5 * (attempt + 1)
                time.sleep(min(wait, 30.0))
                continue
            if r.status_code in (500, 502, 503, 504):
                with self._lock:
                    self.server_errors += 1
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 403:
                with self._lock:
                    self.forbidden += 1
                return None
            # 404 (missing tag/club) -> give up quietly
            return None
        return None

    # Tags come from the API with a leading '#'; encode as %23.
    @staticmethod
    def _tag(tag: str) -> str:
        return "%23" + tag.lstrip("#").upper()

    def battlelog(self, tag: str):
        return self._get(f"players/{self._tag(tag)}/battlelog")

    def player(self, tag: str):
        return self._get(f"players/{self._tag(tag)}")

    def club(self, tag: str):
        return self._get(f"clubs/{self._tag(tag)}")

    def top_players(self, country: str = "global"):
        return self._get(f"rankings/{country}/players")

    def top_brawler_players(self, brawler_id: int, country: str = "global"):
        return self._get(f"rankings/{country}/brawlers/{brawler_id}")

    def top_clubs(self, country: str = "global"):
        return self._get(f"rankings/{country}/clubs")

    def brawlers(self):
        return self._get("brawlers")
