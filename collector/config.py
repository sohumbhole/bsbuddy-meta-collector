"""Configuration + constants for the BS Buddy meta collector.

Ports the app's collection/scoring constants so the server-side snapshot is
built the exact same way the app would build it locally.
"""
import os

# --- API ---
PROXY_BASE = "https://bsproxy.royaleapi.dev/v1"
# The key is read from the environment (a GitHub Actions Secret). NEVER commit it.
API_KEY = os.environ.get("BS_PROXY_KEY", "")

# Politeness / rate limiting. LIVE FINDING (2026-07-08, session 3): after the
# limiter-lock + connection-pool fixes, throughput is bottlenecked by
# CONCURRENCY x request LATENCY, NOT by the rate cap and NOT by throttling. At
# concurrency 16 we saw ~4.7 rps with ZERO 429s and ZERO 403s across many
# passes, implying ~3.4s/request through the proxy (16 / 4.7). The rate limiter
# (was 10) never even binds. The x-ratelimit-limit header is NOT forwarded by
# the proxy (came back absent), so we can't read the ceiling directly - instead
# we PROBE it by scaling concurrency and watching the 429 counter, with the
# bounded 429 auto-slowdown + Retry-After as the safety net.
# So: raise CONCURRENCY (the actual throughput lever) to ~48 to reach ~14 rps
# (48 / 3.4), and keep a comfortably-above rps cap so the limiter only engages
# if latency drops. Zero throttling at 4.7 means real headroom; if the next
# passes still show 0 x 429, push concurrency higher again. Override live via
# BS_CONCURRENCY / BS_RPS env vars (repo variables) without a code change.
MAX_CONCURRENCY = int(os.environ.get("BS_CONCURRENCY", "48"))
TARGET_RPS = float(os.environ.get("BS_RPS", "15"))
REQUEST_TIMEOUT = 15

# --- Run budget (one Actions job is bounded; runs accumulate via committed state) ---
TIME_BUDGET_SECONDS = int(os.environ.get("BS_TIME_BUDGET", "1200"))  # 20 min default

# --- Collection tuning (mirrors the Swift MetaService) ---
# Re-check a KNOWN player after only 4h (was 20h): once we've saturated the
# reachable high-rank pool, re-polling active players for their NEW games is the
# cheapest way to keep collecting (they play again within hours). Sohum: "if
# it's been >4h and they're active, it's fair game, no harm." A player who has
# gone DORMANT (no fresh games from them in ~2 days) is unlikely to play soon,
# so they get a long cooldown to avoid wasting calls; they still get re-checked
# if they resurface in a future match.
REFETCH_COOLDOWN_HOURS = 4
DORMANT_AFTER_HOURS = 48             # no fresh games in this long => dormant
DORMANT_COOLDOWN_HOURS = 72          # dormant players: only re-check this often
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
# Tournaments cache (Q3): Liquipedia wikitext fetched once/day, published as a
# GitHub Release asset (gz); the app downloads + parses it with its own parser.
TOURNAMENTS_GZ_FILE = f"{DATA_DIR}/tournaments.json.gz"
TOURNAMENT_FETCH_INTERVAL_HOURS = 24   # hit Liquipedia at most once a day
TOURNAMENT_RETRY_INTERVAL_HOURS = 2    # if a fetch fails, retry no sooner than this

SEEN_CAP = 400_000                                # bound the dedupe set
# Bigger rotation pool = more distinct players fetchable within the 20h refetch
# window before we start re-treading (the current throughput ceiling). These
# files are small JSON committed to git; 150k tags is still only a few MB.
PRODUCER_CAP = 150_000                             # bound the roster
ELITE_CAP = 120_000                                # bound the elite pool

# snapshot.json is published as a GitHub Release asset (2GB cap), not a
# git-committed blob (git/GitHub hard-rejects any committed file over ~100MiB,
# which is what caused the 2026-07-07 outage).
#
# Sohum's directive (2026-07-08): DO NOT prune valuable current data to hit a
# size target. We never want to lose a real data point until it ages out; the
# ONLY thing that should remove data is the half-life decay (model.py), which
# deletes a tally once its weight falls below ~0.05 (roughly 5 weeks of no new
# games for that pair). That age-based decay is what actually bounds the file:
# real versus/synergy keys are capped by brawler-pair combinatorics per map, so
# with constant inflow the size PLATEAUS where decay-out balances new-in, not
# grows without limit. The 38% compaction (model.compact_snapshot) plus decay
# keep the plateau modest.
#
# enforce_size_budget() is therefore demoted to a pure RUNAWAY-BUG BACKSTOP: it
# must not fire on healthy growth, only if a bug spins forever appending garbage
# keys. Set the net far above any plausible healthy plateau so normal data is
# never touched. (This raw number also feeds the phone's parse-memory limit and
# the 2GB gz Release cap; both are tracked as the storage-scale backlog item in
# STATE.md / FABLE_RESEARCH.md, which is the real long-term fix, not pruning.)
SNAPSHOT_MAX_BYTES = int(os.environ.get("BS_SNAPSHOT_MAX_BYTES", "1400000000"))  # 1.4GB backstop
