"""Entrypoint: load committed state, decay by elapsed time + patches, crawl for
the time budget, aggregate, and write the fresh interchange snapshot + state.

Run:  python -m collector.main
The small state files (data/*.json besides snapshot.json) are committed back
to the repo by the GitHub Action so runs accumulate. snapshot.json has no size
ceiling by design, so it is published as a GitHub Release asset instead (git
hard-rejects committed blobs over ~100MiB): the Action gzips+uploads
data/snapshot.json.gz, which the app downloads and decompresses (see
SharedSnapshot.swift's Gunzip). BS_PROXY_KEY must be set in the environment
(an Actions Secret).
"""
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import config
from .api import Api
from .crawl import Crawler
from . import model
from . import stats as statsmod


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def load_state():
    elite_raw = _load(config.ELITE_FILE, {})
    elite = {
        "tags": set(elite_raw.get("tags", [])),
        "cursor": elite_raw.get("cursor", 0),
        "clubs": set(elite_raw.get("clubs", [])),
        "clubExpandedAt": elite_raw.get("clubExpandedAt", {}),
    }
    return {
        "snapshot": _load(config.SNAPSHOT_FILE, {"maps": {}}),
        "producers": _load(config.PRODUCERS_FILE, {}),
        "elite": elite,
        "seen": set(_load(config.SEEN_FILE, [])),
        "meta": _load(config.STATE_FILE, {}),
    }


def _save_gzip(path, data):
    """Plain gzip.compress: no FNAME/FCOMMENT/FEXTRA, deterministic 10-byte
    header, so the app's minimal gunzip decoder has a simple format to walk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(gzip.compress(payload))
    os.replace(tmp, path)


def save_state(state):
    _save(config.SNAPSHOT_FILE, state["snapshot"])
    _save_gzip(config.SNAPSHOT_GZ_FILE, state["snapshot"])
    # bound the growing sets/maps before persisting
    seen = list(state["seen"])[-config.SEEN_CAP:]
    _save(config.SEEN_FILE, seen)
    producers = state["producers"]
    if len(producers) > config.PRODUCER_CAP:
        ranked = sorted(producers.items(),
                        key=lambda kv: (kv[1].get("timesFetched", 0),
                                        kv[1].get("totalGamesSeen", 0)), reverse=True)
        producers = dict(ranked[:config.PRODUCER_CAP])
    _save(config.PRODUCERS_FILE, producers)
    tags = list(state["elite"]["tags"])[-config.ELITE_CAP:]
    _save(config.ELITE_FILE, {
        "tags": tags,
        "cursor": state["elite"]["cursor"],
        "clubs": list(state["elite"]["clubs"]),
        "clubExpandedAt": state["elite"]["clubExpandedAt"],
    })
    _save(config.STATE_FILE, state["meta"])


STATS_FILE = f"{config.DATA_DIR}/stats.json"
STATS_HISTORY_CAP = 3000


def write_stats(state, crawler, now, new_games, new_ranked, new_high, api_calls, snapshot_size_bytes):
    names = {str(b["id"]): b["name"] for b in
             ((crawler.api.brawlers() or {}).get("items", []))}
    totals, findings = statsmod.summarize(state["snapshot"], names)
    stats = _load(STATS_FILE, {"history": []})
    stats["updatedAt"] = now.isoformat().replace("+00:00", "Z")
    stats["current"] = {
        **totals,
        "elitePool": len(state["elite"]["tags"]),
        "eliteClubs": len(state["elite"]["clubs"]),
        "lastRunNewGames": new_games,
        "lastRunNewHighRank": new_high,
        "lastRunApiCalls": api_calls,
        # Raw (uncompressed) snapshot size, same measure enforce_size_budget()
        # uses. The safety-net cap (SNAPSHOT_MAX_BYTES) is on THIS number, not
        # the gzipped upload size, since it's what the phone downloads-after-
        # decompression and parses. Surfaced so Sohum can see it climbing
        # toward the cap on the dashboard rather than finding out from a
        # failed push.
        "snapshotSizeBytes": snapshot_size_bytes,
        "snapshotSizeCapBytes": config.SNAPSHOT_MAX_BYTES,
    }
    stats["findings"] = findings
    stats["history"].append({
        "ts": stats["updatedAt"],
        "totalGames": totals["totalGames"],
        "rankedGames": totals["rankedGames"],
        "highRankGames": totals["highRankGames"],
        "quality": totals["quality"],
        "newGames": new_games,
        "newRanked": new_ranked,
        "newHighRank": new_high,
        "apiCalls": api_calls,
        "elitePool": len(state["elite"]["tags"]),
        "eliteClubs": len(state["elite"]["clubs"]),
    })
    stats["history"] = stats["history"][-STATS_HISTORY_CAP:]
    _save(STATS_FILE, stats)


def main():
    if not config.API_KEY:
        print("ERROR: BS_PROXY_KEY not set in the environment.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    state = load_state()
    last_run = state["meta"].get("lastRunAt")

    # 1) age the existing snapshot (1-week half-life) + patch boundaries
    model.apply_time_decay(state["snapshot"], last_run, now)
    model.apply_patch_decay(state["snapshot"], last_run, now)

    # 2) crawl
    api = Api()
    crawler = Crawler(api, state)
    if len(state["elite"]["tags"]) < 3000:
        print("Harvesting elite pool (cold start)...")
        crawler.harvest_elite()
    deadline = time.monotonic() + config.TIME_BUDGET_SECONDS
    print(f"Crawling for up to {config.TIME_BUDGET_SECONDS}s...")
    crawler.run(deadline)

    # 3) aggregate new games into the snapshot
    model.aggregate_into(state["snapshot"], crawler.games)
    model.finalize(state["snapshot"], now)

    # 3b) enforce the size budget every run so the file never creeps back over
    # GitHub's 100MB push limit (root cause of the 2026-07-07 outage).
    pruned_size = model.enforce_size_budget(state["snapshot"])
    print(f"snapshot.json budget check: {pruned_size / 1_000_000:.1f}MB "
          f"(cap {config.SNAPSHOT_MAX_BYTES / 1_000_000:.0f}MB)")

    # 4) dashboard stats (small stats.json the website reads, not the big blob)
    ranked = sum(1 for g in crawler.games if g["is_ranked"])
    high_rank = sum(1 for g in crawler.games if g["rank_stage"] >= config.HIGH_STAGE_FLOOR)
    write_stats(state, crawler, now, len(crawler.games), ranked, high_rank, api.calls, pruned_size)

    # 5) persist
    state["meta"]["lastRunAt"] = now.isoformat()
    state["meta"]["totalCalls"] = state["meta"].get("totalCalls", 0) + api.calls
    save_state(state)

    high_rank = sum(1 for g in crawler.games if g["rank_stage"] >= config.HIGH_STAGE_FLOOR)
    ranked = sum(1 for g in crawler.games if g["is_ranked"])
    print(f"Done. api_calls={api.calls} new_games={len(crawler.games)} "
          f"ranked={ranked} high_rank(legend+)={high_rank} "
          f"elite_pool={len(state['elite']['tags'])} elite_clubs={len(state['elite']['clubs'])} "
          f"snapshot_games={state['snapshot'].get('gamesAnalyzed')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
