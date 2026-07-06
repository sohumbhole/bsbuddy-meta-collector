"""Thread-pooled, rate-limited client for the official Brawl Stars API via the
RoyaleAPI proxy. Concurrency + a token-bucket limiter + 429 backoff keep us
polite so the shared proxy never bans the key.
"""
import threading
import time
import requests

from . import config


class RateLimiter:
    """Simple token bucket: at most TARGET_RPS requests/second, shared across threads."""
    def __init__(self, rps: float):
        self.min_interval = 1.0 / max(rps, 0.1)
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_time:
                time.sleep(self.next_time - now)
                now = time.monotonic()
            self.next_time = max(now, self.next_time) + self.min_interval

    def slow_down(self, factor: float = 1.5):
        with self.lock:
            self.min_interval *= factor


class Api:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {config.API_KEY}"})
        self.limiter = RateLimiter(config.TARGET_RPS)
        self.calls = 0
        self._lock = threading.Lock()

    def _get(self, path: str):
        """GET with rate limiting + 429/5xx backoff. Returns parsed JSON or None."""
        url = f"{config.PROXY_BASE}/{path}"
        for attempt in range(4):
            self.limiter.acquire()
            with self._lock:
                self.calls += 1
            try:
                r = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
            except requests.RequestException:
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    return None
            if r.status_code == 429:
                self.limiter.slow_down()
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(0.5 * (attempt + 1))
                continue
            # 403 (bad/unwhitelisted key) or 404 (missing tag/club) -> give up quietly
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
