"""Game parsing + aggregation into the shared snapshot, with time + patch decay.

The snapshot mirrors the app's MetaSnapshot: per map, per brawler weighted
tallies (tournament x3 / ranked x2 / ladder x1) plus raw ranked counts split by
rank bucket, and versus/synergy pair tallies. Written as a clean, Python-native
JSON (string keys, floats) that the app adapts into its MetaSnapshot.
"""
import json
from datetime import datetime, timezone
from . import config


def parse_battlelog(log: dict, owner_tag: str) -> list[dict]:
    """One player's battlelog -> list of normalized games we care about."""
    games = []
    for item in (log or {}).get("items", []):
        battle = item.get("battle", {})
        btype = battle.get("type")
        is_ranked = btype in ("soloRanked", "duoRanked")
        teams = battle.get("teams")
        event = item.get("event") or {}
        game_map = event.get("map")
        if not teams or len(teams) != 2 or not game_map:
            continue
        if len(teams[0]) != 3 or len(teams[1]) != 3:
            continue

        # Ladder games only count at high trophies (skip casual noise).
        if not is_ranked:
            if btype != "ranked":
                continue
            trophies = [p.get("brawler", {}).get("trophies")
                        for t in teams for p in t if p.get("brawler")]
            trophies = [x for x in trophies if x is not None]
            if not trophies or sum(trophies) / len(trophies) < config.LADDER_MIN_AVG_TROPHIES:
                continue

        ids = [[p.get("brawler", {}).get("id") for p in t] for t in teams]
        if any(x is None for row in ids for x in row):
            continue

        # Winner (from the owner's perspective).
        winner = None
        result = battle.get("result")
        if result in ("victory", "defeat"):
            owner_first = any(p.get("tag") == owner_tag for p in teams[0])
            if result == "victory":
                winner = 0 if owner_first else 1
            else:
                winner = 1 if owner_first else 0

        # Rank bucket from average rank stage (ranked only).
        bucket = None
        stage = 0
        if is_ranked:
            stages = [p.get("brawler", {}).get("trophies")
                      for t in teams for p in t if p.get("brawler")]
            stages = [x for x in stages if x is not None]
            if stages:
                stage = sum(stages) // len(stages)
                bucket = config.rank_bucket(stage)

        tags = [p.get("tag") for t in teams for p in t]
        dedupe = item.get("battleTime", "") + "".join(sorted(t for t in tags if t))
        games.append({
            "dedupe": dedupe,
            "map": game_map,
            "mode": battle.get("mode") or event.get("mode") or "unknown",
            "teams": ids,
            "winner": winner,
            "is_ranked": is_ranked,
            "rank_bucket": bucket,
            "rank_stage": stage,
            "tags": tags,
        })
    return games


def participant_tags(log: dict) -> list[str]:
    """Tags of players in the owner's RANKED games (queue-jump candidates)."""
    out = []
    for item in (log or {}).get("items", []):
        b = item.get("battle", {})
        if b.get("type") in ("soloRanked", "duoRanked"):
            for team in b.get("teams", []):
                out.extend(p.get("tag") for p in team if p.get("tag"))
    return [t for t in out if t]


# --- Snapshot aggregation ---------------------------------------------------

def _blank_tally():
    return {"picks": 0.0, "wins": 0.0, "rPicks": 0.0, "rWins": 0.0,
            "tPicks": 0.0, "tWins": 0.0, "rankBuckets": {}}


def _bump(tally, weight, won, is_ranked, bucket):
    tally["picks"] += weight
    if won:
        tally["wins"] += weight
    if is_ranked:
        tally["rPicks"] += 1
        if won:
            tally["rWins"] += 1
        if bucket:
            pair = tally["rankBuckets"].get(bucket, [0.0, 0.0])
            pair[0] += 1
            if won:
                pair[1] += 1
            tally["rankBuckets"][bucket] = pair


def aggregate_into(snapshot: dict, games: list[dict]):
    """Fold new games into the snapshot's map/brawler/versus/synergy tallies."""
    maps = snapshot.setdefault("maps", {})
    for g in games:
        if g["winner"] is None:
            continue
        m = maps.setdefault(g["map"], {"mode": g["mode"], "games": 0.0,
                                       "brawlers": {}, "versus": {}, "synergy": {}})
        m["mode"] = g["mode"] or m["mode"]
        m["games"] += 1
        weight = 2 if g["is_ranked"] else 1   # tournament (x3) is ingested app-side
        for ti, team in enumerate(g["teams"]):
            won = g["winner"] == ti
            enemy = g["teams"][1 - ti]
            for b in team:
                bid = str(b)
                t = m["brawlers"].setdefault(bid, _blank_tally())
                _bump(t, weight, won, g["is_ranked"], g["rank_bucket"])
                for e in enemy:
                    key = f"{b}|{e}"
                    vt = m["versus"].setdefault(key, _blank_tally())
                    _bump(vt, weight, won, g["is_ranked"], g["rank_bucket"])
                for a in team:
                    if a == b:
                        continue
                    key = f"{b}|{a}"
                    st = m["synergy"].setdefault(key, _blank_tally())
                    _bump(st, weight, won, g["is_ranked"], g["rank_bucket"])


# --- Decay ------------------------------------------------------------------

def _scale_tally(t, factor):
    # Use .get-style guards: compact_snapshot() drops zero tPicks/tWins to shrink
    # the file, so those keys are frequently ABSENT here. Reading them directly
    # (t["tPicks"]) raised KeyError and crashed every decay pass on a compacted
    # snapshot (2026-07-09 collector freeze). A missing key means 0 -> nothing to
    # scale, so just skip it.
    for k in _TALLY_KEYS:
        if k in t:
            t[k] *= factor
    for bucket, pair in list(t.get("rankBuckets", {}).items()):
        pair[0] *= factor
        pair[1] *= factor
        if pair[0] < 0.05:
            del t["rankBuckets"][bucket]


def _scale_map(m, factor, affected: set[int] | None):
    m["games"] *= factor
    for coll in ("brawlers", "versus", "synergy"):
        for key, t in list(m[coll].items()):
            if affected is not None:
                # Only decay tallies that involve an affected brawler.
                ids = {int(x) for x in key.split("|") if x.lstrip("-").isdigit()}
                if ids.isdisjoint(affected):
                    continue
            _scale_tally(t, factor)
            if t["picks"] < 0.05 and not t["rankBuckets"]:
                del m[coll][key]


def apply_time_decay(snapshot: dict, last_run_iso: str | None, now: datetime):
    """Half-life decay over elapsed real time (Sohum: 1-week half-life)."""
    if not last_run_iso:
        return
    try:
        last = datetime.fromisoformat(last_run_iso)
    except ValueError:
        return
    hours = (now - last).total_seconds() / 3600.0
    if hours <= 0:
        return
    factor = 0.5 ** (hours / (config.HALF_LIFE_DAYS * 24.0))
    if factor >= 0.999:
        return
    for m in snapshot.get("maps", {}).values():
        _scale_map(m, factor, None)


def apply_patch_decay(snapshot: dict, last_run_iso: str | None, now: datetime):
    """Hard-decay across any balance-patch boundary crossed since last run, so
    brawlers aren't judged on pre-patch data (scoped to affected ids if listed)."""
    if not last_run_iso:
        return
    try:
        last = datetime.fromisoformat(last_run_iso)
    except ValueError:
        return
    for date_str in config.PATCH_DATES:
        try:
            patch = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if last < patch <= now:
            affected = config.PATCH_AFFECTED_BRAWLERS.get(date_str)
            affected_set = set(affected) if affected else None
            for m in snapshot.get("maps", {}).values():
                _scale_map(m, config.PATCH_HARD_DECAY, affected_set)


def _snapshot_size(snapshot: dict) -> int:
    return len(json.dumps(snapshot, separators=(",", ":")))


_TALLY_KEYS = ("picks", "wins", "rPicks", "rWins", "tPicks", "tWins")


def compact_snapshot(snapshot: dict, ndigits: int = 2):
    """Shrink the interchange file WITHOUT losing meaningful precision (~38%):
    the decay math produces absurd floats like 1418.5006179173342 and always-
    zero tournament fields. Round tally values to `ndigits` and drop zero
    tPicks/tWins (the app defaults missing keys to 0). Safe + app-compatible;
    versus/synergy pairs are 99% of the size so this is where it matters."""
    for m in snapshot.get("maps", {}).values():
        for coll in ("brawlers", "versus", "synergy"):
            for t in m.get(coll, {}).values():
                for k in _TALLY_KEYS:
                    if k in t:
                        t[k] = round(t[k], ndigits)
                if not t.get("tPicks"):
                    t.pop("tPicks", None)
                if not t.get("tWins"):
                    t.pop("tWins", None)
                rb = t.get("rankBuckets")
                if rb:
                    for b, pair in rb.items():
                        rb[b] = [round(pair[0], ndigits), round(pair[1], ndigits)]


def enforce_size_budget(snapshot: dict, max_bytes: int = config.SNAPSHOT_MAX_BYTES) -> int:
    """RUNAWAY-BUG BACKSTOP ONLY. Not a routine prune.

    Sohum's directive (2026-07-08): never shed valuable current data to hit a
    size target; the ONLY thing that removes data is the half-life decay above.
    So SNAPSHOT_MAX_BYTES is set far above any healthy plateau and this function
    is a no-op under normal operation, returning the size unchanged. It only
    fires if a bug spins forever appending garbage keys (the kind of failure
    that on 2026-07-07 pushed the file past the old git-blob limit). In that
    pathological case it ratchets a picks-floor up, dropping the weakest
    (lowest-signal) pairs first and re-measuring, purely to keep the Release
    asset uploadable. Returns the final size in bytes.
    """
    size = _snapshot_size(snapshot)
    if size <= max_bytes:
        return size
    for floor in (1, 2, 4, 8, 16, 32, 64, 128, 256):
        for m in snapshot.get("maps", {}).values():
            for coll in ("versus", "synergy"):
                d = m.get(coll, {})
                for key in [k for k, t in d.items() if t["picks"] < floor]:
                    del d[key]
        size = _snapshot_size(snapshot)
        if size <= max_bytes:
            return size
    # Last resort (should not happen given the ratchet above): drop whole
    # maps with the least data, lowest-signal first, until under budget.
    by_games = sorted(snapshot.get("maps", {}).items(), key=lambda kv: kv[1].get("games", 0.0))
    for map_name, _ in by_games:
        del snapshot["maps"][map_name]
        size = _snapshot_size(snapshot)
        if size <= max_bytes:
            break
    return size


def build_lite_snapshot(snapshot: dict) -> dict:
    """A copy of the snapshot with the versus/synergy pair collections dropped
    from every map, keeping only per-brawler tallies + map metadata.

    This is the tier-list / meta-hub feed: TierListBuilder reads only
    `stats.brawlers`, never the pairs, yet the pairs are ~99% of the file.
    Shipping this ~1%-size slice as its own Release asset lets the app parse
    tier lists cheaply on the phone (parse memory is the binding constraint,
    not the 2GB asset cap). The full snapshot still publishes for the draft/
    matchup screens. Top-level metadata is copied through so the app's existing
    adapter parses it unchanged (its parser already treats absent versus/
    synergy as empty)."""
    lite = {k: v for k, v in snapshot.items() if k != "maps"}
    lite_maps = {}
    for name, m in snapshot.get("maps", {}).items():
        lite_maps[name] = {
            "mode": m.get("mode"),
            "games": m.get("games", 0.0),
            "brawlers": m.get("brawlers", {}),
        }
    lite["maps"] = lite_maps
    return lite


def finalize(snapshot: dict, now: datetime):
    """Stamp metadata + recompute the active game count for the interchange file."""
    snapshot["generatedAt"] = now.isoformat().replace("+00:00", "Z")
    snapshot["windowDays"] = config.HALF_LIFE_DAYS
    total = sum(m.get("games", 0.0) for m in snapshot.get("maps", {}).values())
    snapshot["gamesAnalyzed"] = round(total)
    return snapshot
