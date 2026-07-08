"""Cheap summary stats + fun findings from the snapshot, for the live
dashboard. No heavy scoring here (the app does the real math); this is just
"is it working and what did we find" at a glance.
"""
from . import config

HIGH_BUCKETS = ("legendary", "masters", "pro")


def _brawler_rollup(snapshot):
    """Per-brawler high-rank picks/wins + total ranked picks, across all maps."""
    roll = {}  # bid -> {"hrPicks","hrWins","rPicks","picks"}
    for m in snapshot.get("maps", {}).values():
        for bid, t in m.get("brawlers", {}).items():
            r = roll.setdefault(bid, {"hrPicks": 0.0, "hrWins": 0.0, "rPicks": 0.0, "picks": 0.0})
            r["picks"] += t.get("picks", 0.0)
            r["rPicks"] += t.get("rPicks", 0.0)
            for b in HIGH_BUCKETS:
                pair = t.get("rankBuckets", {}).get(b)
                if pair:
                    r["hrPicks"] += pair[0]
                    r["hrWins"] += pair[1]
    return roll


# Quality tuning.
HR_VOLUME_TARGET = 200_000     # high-rank games where the volume component tops out
ACTIVE_MAP_MIN_HR = 50         # a map "counts" once it has this many high-rank games
CELL_PLAYED_MIN = 30           # a (map,brawler) cell is eligible once actually played this much
CELL_COVERED_MIN = 15          # ...and "covered" once it has this many high-rank games
ROTATION_FRACTION = 0.15       # a map is "in rotation" at >=15% of the busiest map's high-rank games
# The tier list is offered per rank (gold and up). Quality now also rewards
# having a BASELINE in every rank (P1.4), so adding lower-rank data IMPROVES the
# score instead of dragging down the old "% high-rank focus" metric. A bucket
# tops out its contribution at this many game-equivalents.
TIER_BUCKETS = ("gold", "diamond", "mythic", "legendary", "masters", "pro")
BUCKET_BASELINE = 8_000


def summarize(snapshot, names):
    """Returns (totals, findings). names = {id_str: brawler_name}."""
    maps = snapshot.get("maps", {})
    total_games = sum(m.get("games", 0.0) for m in maps.values())

    ranked_appearances = 0.0
    high_appearances = 0.0
    # First pass: high-rank games per map (to find which maps are IN ROTATION).
    map_hr_games = {}
    for mp, m in maps.items():
        hr = 0.0
        for t in m.get("brawlers", {}).values():
            ranked_appearances += t.get("rPicks", 0.0)
            h = sum((t.get("rankBuckets", {}).get(b, [0, 0])[0]) for b in HIGH_BUCKETS)
            high_appearances += h
            hr += h
        map_hr_games[mp] = hr / 6.0

    # A map is "in rotation" if it has a meaningful share of the busiest map's
    # recent high-rank games. Because we keep only ~a week of data (half-life),
    # this IS the current rotation: maps that rotated out decay below the floor
    # and stop counting. Quality is judged ONLY on these maps (Sohum: it can't
    # be judged on the 96 maps not in rotation, which by design have no recent
    # data). The floor scales with volume so it stays right as data grows.
    top = max(map_hr_games.values(), default=0.0)
    rotation_floor = max(ACTIVE_MAP_MIN_HR, ROTATION_FRACTION * top)
    rotation_maps = [mp for mp, hr in map_hr_games.items() if hr >= rotation_floor]

    eligible_cells = 0     # brawler actually played on a rotation map
    covered_cells = 0      # ...and has enough high-rank games there
    for mp in rotation_maps:
        for t in maps[mp].get("brawlers", {}).values():
            if t.get("picks", 0.0) < CELL_PLAYED_MIN:
                continue
            eligible_cells += 1
            hr = sum((t.get("rankBuckets", {}).get(b, [0, 0])[0]) for b in HIGH_BUCKETS)
            if hr >= CELL_COVERED_MIN:
                covered_cells += 1
    active_maps = len(rotation_maps)

    # 6 brawler appearances per game -> divide to get game-equivalents.
    ranked_games = ranked_appearances / 6.0
    high_rank_games = high_appearances / 6.0
    hr_focus = high_rank_games / ranked_games if ranked_games else 0.0

    # Per-tier-bucket game-equivalents, for the RANK BASELINE metric.
    bucket_games = {b: 0.0 for b in TIER_BUCKETS}
    for m in maps.values():
        for t in m.get("brawlers", {}).values():
            rb = t.get("rankBuckets", {})
            for b in TIER_BUCKETS:
                bucket_games[b] += (rb.get(b, [0, 0])[0])
    bucket_games = {b: v / 6.0 for b, v in bucket_games.items()}
    # Soft baseline coverage: each bucket contributes up to 1 as it fills toward
    # BUCKET_BASELINE; the mean across the 6 tier ranks is the rank-baseline
    # score. Adding lower-rank data can only RAISE this (never lowers volume/
    # coverage), so more complete data always scores >= before.
    rank_baseline = sum(min(1.0, bucket_games[b] / BUCKET_BASELINE)
                        for b in TIER_BUCKETS) / len(TIER_BUCKETS)

    # Data quality (0-100): high-rank VOLUME + high-rank map COVERAGE stay the
    # dominant drivers (0.70) so pro/high-level data remains the priority; the
    # RANK BASELINE (0.30) rewards having usable data in every rank so a
    # gold/diamond player also gets a real tier list. This REPLACES the old
    # "% of data that is Legendary+" term, which perversely fell as we (rightly)
    # added lower-rank baselines.
    volume = min(1.0, high_rank_games / HR_VOLUME_TARGET)
    coverage = (covered_cells / eligible_cells) if eligible_cells else 0.0
    quality = round(100 * (0.45 * volume + 0.25 * coverage + 0.30 * rank_baseline))

    totals = {
        "totalGames": round(total_games),
        "rankedGames": round(ranked_games),
        "highRankGames": round(high_rank_games),
        "highRankFocus": round(100 * hr_focus),   # % of ranked data that is Legendary+ (info only)
        "rankBaseline": round(100 * rank_baseline),  # % of tier ranks with a usable baseline
        "bucketGames": {b: round(v) for b, v in bucket_games.items()},
        "maps": len(maps),
        "activeMaps": active_maps,
        "coveredCells": covered_cells,
        "totalCells": eligible_cells,
        "quality": quality,
    }

    thin = [b for b in TIER_BUCKETS if bucket_games[b] < BUCKET_BASELINE]
    findings = [{"label": "Rank baseline",
                 "value": (f"{round(100 * rank_baseline)}% of tier ranks covered"
                           + (f"; thin: {', '.join(thin)}" if thin else "; all ranks covered"))}]
    findings += _findings(snapshot, names)
    return totals, findings


def _findings(snapshot, names):
    roll = _brawler_rollup(snapshot)

    def name(bid):
        return names.get(str(bid), f"#{bid}")

    out = []

    # Most-picked brawler in Legendary+.
    most = sorted(roll.items(), key=lambda kv: kv[1]["hrPicks"], reverse=True)
    if most and most[0][1]["hrPicks"] > 0:
        out.append({"label": "Most picked (Legendary+)",
                    "value": f"{name(most[0][0])} · {round(most[0][1]['hrPicks'] / 6):,} games"})

    # Highest win rate in Legendary+ (min sample).
    rated = [(bid, r["hrWins"] / r["hrPicks"]) for bid, r in roll.items() if r["hrPicks"] >= 300]
    rated.sort(key=lambda kv: kv[1], reverse=True)
    if rated:
        out.append({"label": "Top win rate (Legendary+)",
                    "value": f"{name(rated[0][0])} · {round(rated[0][1] * 100)}%"})
        out.append({"label": "Lowest win rate (Legendary+)",
                    "value": f"{name(rated[-1][0])} · {round(rated[-1][1] * 100)}%"})

    # Most-contested map (most high-rank games).
    map_hr = []
    for mp, m in snapshot.get("maps", {}).items():
        hr = sum(sum(t.get("rankBuckets", {}).get(b, [0, 0])[0] for b in HIGH_BUCKETS)
                 for t in m.get("brawlers", {}).values())
        map_hr.append((mp, hr / 6.0))
    map_hr.sort(key=lambda kv: kv[1], reverse=True)
    if map_hr and map_hr[0][1] > 0:
        out.append({"label": "Most data on map",
                    "value": f"{map_hr[0][0]} · {round(map_hr[0][1]):,} games"})

    return out
