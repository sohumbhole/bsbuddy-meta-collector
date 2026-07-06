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


def summarize(snapshot, names):
    """Returns (totals, findings). names = {id_str: brawler_name}."""
    maps = snapshot.get("maps", {})
    total_games = sum(m.get("games", 0.0) for m in maps.values())

    ranked_appearances = 0.0
    high_appearances = 0.0
    covered_cells = 0
    total_cells = 0
    for m in maps.values():
        for t in m.get("brawlers", {}).values():
            ranked_appearances += t.get("rPicks", 0.0)
            hr = sum((t.get("rankBuckets", {}).get(b, [0, 0])[0]) for b in HIGH_BUCKETS)
            high_appearances += hr
            total_cells += 1
            if hr >= 15:            # >=15 high-rank games featuring this brawler here
                covered_cells += 1

    # 6 brawler appearances per game -> divide to get game-equivalents.
    ranked_games = ranked_appearances / 6.0
    high_rank_games = high_appearances / 6.0

    # Data quality (heuristic, 0-100): how much high-rank data + how broadly
    # it covers the map x brawler grid.
    volume = min(1.0, high_rank_games / 200_000.0)
    coverage = (covered_cells / total_cells) if total_cells else 0.0
    quality = round(100 * (0.6 * volume + 0.4 * coverage))

    totals = {
        "totalGames": round(total_games),
        "rankedGames": round(ranked_games),
        "highRankGames": round(high_rank_games),
        "maps": len(maps),
        "coveredCells": covered_cells,
        "totalCells": total_cells,
        "quality": quality,
    }

    findings = _findings(snapshot, names)
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
