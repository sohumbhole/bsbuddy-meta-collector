"""Configuration + constants for the BS Buddy meta collector.

Ports the app's collection/scoring constants so the server-side snapshot is
built the exact same way the app would build it locally.
"""
import os

# --- API ---
PROXY_BASE = "https://bsproxy.royaleapi.dev/v1"
# The key is read from the environment (a GitHub Actions Secret). NEVER commit it.
API_KEY = os.environ.get("BS_PROXY_KEY", "")

# Politeness / rate limiting. The rate that binds is Supercell's per-key limit
# on OUR key (the RoyaleAPI proxy is a transparent passthrough). Fable research
# Q2: community consensus is ~10 req/s per token, throttling (not banning) is
# the failure mode, and the API reports the true ceiling in the x-ratelimit-limit
# header (now logged as limitHeader). So we target 10 rps / concurrency 16 (the
# safe-aggressive sweet spot) - roughly 2x our previously-measured ~4.8 rps once
# the throughput bugs are fixed (limiter lock + connection pool). RAISE these
# only after the logged x-ratelimit-limit shows real headroom; the 429 handler
# auto-slows (bounded) and honors Retry-After if we overshoot. Override live via
# the BS_CONCURRENCY / BS_RPS env vars without a code change.
MAX_CONCURRENCY = int(os.environ.get("BS_CONCURRENCY", "16"))
TARGET_RPS = float(os.environ.get("BS_RPS", "10"))
REQUEST_TIMEOUT = 15

# --- Run budget (one Actions job is bounded; runs accumulate via committed state) ---
TIME_BUDGET_SECONDS = int(os.environ.get("BS_TIME_BUDGET", "1200"))  # 20 min default

# --- Collection tuning (mirrors the Swift MetaService) ---
REFETCH_COOLDOWN_HOURS = 20          # a log barely changes inside a day
LADDER_MIN_AVG_TROPHIES = 650        # skip casual trophy noise
HIGH_STAGE_FLOOR = 16                # Legendary+ = the rare high-rank gold
ELITE_CLUB_FLOOR = 700_000           # club total trophies to count as elite
CLUB_RECHECK_COOLDOWN_HOURS = 96     # don't re-walk a club within 4 days
ELITE_MEMBER_TROPHY_FLOOR = 20_000   # drop obvious alts from harvested clubs

HARVEST_COUNTRIES = [
    "global", "us", "de", "kr", "jp", "br", "fr", "gb", "es", "ru",
    "tr", "mx", "it", "pl", "id", "ca", "au", "nl", "se", "ph",
]

# Rank stage -> coarse bucket (Bronze/Silver fold into gold).
def rank_bucket(stage: int) -> str:
    if stage < 10:
        return "gold"
    if stage <= 12:
        return "diamond"
    if stage <= 15:
        return "mythic"
    if stage <= 18:
        return "legendary"
    if stage <= 21:
        return "masters"
    return "pro"

# --- Meta freshness ---
# One-week half-life: every game's weight halves after 7 days. Applied to the
# whole snapshot each run based on elapsed time (Sohum: half-life = 1 week).
HALF_LIFE_DAYS = 7.0

# Balance-patch boundaries (UTC dates, YYYY-MM-DD). When a run crosses one, we
# hard-decay so brawlers aren't judged on pre-patch data. Keep this list
# updated as patches drop. Optionally scope to affected brawler ids.
PATCH_DATES = [
    # "2026-06-15",
]
PATCH_HARD_DECAY = 0.30              # multiply tallies by this at a patch boundary
# Optional: {"2026-06-15": [16000000, 16000001]} to only decay affected brawlers.
PATCH_AFFECTED_BRAWLERS: dict[str, list[int]] = {}

# --- State files (the small ones committed to the repo so runs accumulate) ---
DATA_DIR = os.environ.get("BS_DATA_DIR", "data")
SNAPSHOT_FILE = f"{DATA_DIR}/snapshot.json"       # full interchange (local/debug only, not committed)
SNAPSHOT_GZ_FILE = f"{DATA_DIR}/snapshot.json.gz"  # published as a GitHub Release asset (app downloads this)
PRODUCERS_FILE = f"{DATA_DIR}/producers.json"     # producer roster
ELITE_FILE = f"{DATA_DIR}/elite_pool.json"        # elite pool + clubs + cursor
SEEN_FILE = f"{DATA_DIR}/seen.json"               # bounded game dedupe keys
STATE_FILE = f"{DATA_DIR}/state.json"             # lastRunAt etc.

SEEN_CAP = 400_000                                # bound the dedupe set
PRODUCER_CAP = 60_000                             # bound the roster
ELITE_CAP = 60_000                                # bound the elite pool

# snapshot.json is now published as a GitHub Release asset (2GB cap), not a
# git-committed blob (git/GitHub hard-rejects any committed file over ~100MiB,
# which is what caused the 2026-07-07 outage: growth is uncapped by design so
# Sohum can collect as many games as the API allows, no artificial ceiling).
# This is a SAFETY NET only (a bug that spins forever appending garbage keys),
# not a routine data-destroying prune: real versus/synergy keys are bounded by
# brawler-pair combinatorics per map (~a few hundred MB at total saturation),
# comfortably under this. enforce_size_budget() is a no-op unless something is
# actually wrong.
SNAPSHOT_MAX_BYTES = 900_000_000
