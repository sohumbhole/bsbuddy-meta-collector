"""Server-side Liquipedia tournament fetcher (Q3).

Fetches Brawl Stars pro tournaments from Liquipedia's FREE, KEYLESS MediaWiki
api.php ONCE PER DAY on GitHub's IP, caching the raw wikitext + category/subpage
lists into an interchange blob the app downloads. The app then reuses its OWN
existing wikitext parser (BracketParser / parseEvent) fed from this cache and
NEVER calls Liquipedia itself, so phones stop getting IP-banned.

COMPLIANCE (Liquipedia API terms of use, verified 2026-07-08):
- Custom User-Agent that identifies the project + a contact channel is
  MANDATORY (generic agents like python-requests are auto-blocked; that was the
  likely real cause of the phone bans, not volume).
- Max 1 request / 2 seconds for api.php. We make ~12-40 requests total, once a
  day, so we are ~3 orders of magnitude under their ceiling.
- action=parse is the resource-intensive one (1/30s); we only use
  action=query (revisions/categorymembers/allpages), the ordinary tier.
- Honor 429 by stopping this cycle (never hammer); try again tomorrow.
"""
import time
from datetime import datetime, timezone

import requests

API = "https://liquipedia.net/brawlstars/api.php"
UA = ("BSBuddyCollector/1.0 "
      "(https://github.com/sohumbhole/bsbuddy-meta-collector; "
      "personal Brawl Stars companion app; contact via repo issues)")
# Liquipedia REQUIRES gzip: api.php returns 406 without Accept-Encoding: gzip.
# The `requests` library sends it and auto-decompresses; do NOT hand-roll this.

MIN_INTERVAL = 2.1          # >= 1 req / 2s per the terms, with margin
MAX_EVENTS = 24             # matches the app's list depth
MAX_SUBPAGES_PER_EVENT = 8
WIKITEXT_BATCH = 40         # up to 50 titles per revisions query; stay under


class RateLimited(Exception):
    pass


class _Fetcher:
    def __init__(self):
        self._last = 0.0
        self._session = requests.Session()
        # requests auto-sends Accept-Encoding: gzip (satisfies Liquipedia's 406
        # gzip requirement) and auto-decompresses.
        self._session.headers.update({"User-Agent": UA})

    def _get(self, params: dict):
        # Space requests >= MIN_INTERVAL apart (single-threaded, once/day).
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            r = self._session.get(API, params=params, timeout=30)
        except requests.RequestException:
            return None
        if r.status_code == 429:
            raise RateLimited()
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def category_members(self, category: str) -> list:
        data = self._get({
            "action": "query", "list": "categorymembers",
            "cmtitle": f"Category:{category}", "cmlimit": "20",
            "cmsort": "timestamp", "cmdir": "desc", "format": "json",
        })
        members = (((data or {}).get("query") or {}).get("categorymembers")) or []
        return [m["title"] for m in members if "title" in m]

    def subpages(self, page: str) -> list:
        data = self._get({
            "action": "query", "list": "allpages",
            "apprefix": f"{page}/", "aplimit": "10", "format": "json",
        })
        pages = (((data or {}).get("query") or {}).get("allpages")) or []
        titles = [p["title"] for p in pages if "title" in p]
        return [t for t in titles if "Statistics" not in t and "Participants" not in t]

    def wikitexts(self, titles: list) -> dict:
        out = {}
        for i in range(0, len(titles), WIKITEXT_BATCH):
            batch = titles[i:i + WIKITEXT_BATCH]
            data = self._get({
                "action": "query", "prop": "revisions", "rvprop": "content",
                "rvslots": "main", "titles": "|".join(batch),
                "format": "json", "formatversion": "2",
            })
            for page in (((data or {}).get("query") or {}).get("pages")) or []:
                revs = page.get("revisions") or []
                if revs:
                    content = (((revs[0].get("slots") or {}).get("main") or {}).get("content"))
                    if content:
                        out[page["title"]] = content
        return out


def fetch() -> dict | None:
    """Returns the interchange dict, or None if Liquipedia was unreachable /
    rate-limited (caller keeps yesterday's cache and retries tomorrow)."""
    f = _Fetcher()
    try:
        categories = {}
        event_titles = []
        for category, tier in [("S-Tier_Tournaments", "S-TIER"),
                               ("A-Tier_Tournaments", "A-TIER")]:
            titles = [t for t in f.category_members(category)
                      if not any(bad in t for bad in ("Showmatch", "Overview", "Awards"))
                      and not t.startswith("Category:")]
            categories[tier] = titles
            event_titles.extend(titles)

        if not event_titles:
            return None  # couldn't reach Liquipedia at all

        event_titles = event_titles[:MAX_EVENTS]

        # Subpages (brackets often live on /Group_Stage, /Playoffs, etc.)
        subpages = {}
        all_pages = list(event_titles)
        for page in event_titles:
            subs = f.subpages(page)[:MAX_SUBPAGES_PER_EVENT]
            if subs:
                subpages[page] = subs
                all_pages.extend(subs)

        wikitext = f.wikitexts(all_pages)

        # Only list events we actually cached wikitext for (so the app never
        # references an event it can't parse).
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "categories": {k: [t for t in v if t in wikitext] for k, v in categories.items()},
            "subpages": subpages,
            "wikitext": wikitext,
        }
    except RateLimited:
        print("::warning::Liquipedia returned 429; skipping tournament fetch this cycle")
        return None
