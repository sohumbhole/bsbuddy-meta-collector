"""The crawl engine: seed from the elite pool + high-rank producers, snowball
through ranked co-players, and expand elite clubs recursively. Ports the app's
MetaService strategy to run server-side under a time budget.
"""
import time
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config
from .model import parse_battlelog, participant_tags


class Crawler:
    def __init__(self, api, state):
        self.api = api
        self.producers = state["producers"]      # tag -> record
        self.elite = state["elite"]              # {"tags": set, "cursor": int,
                                                 #  "clubs": set, "clubExpandedAt": {tag: iso}}
        self.seen = state["seen"]                # set of game dedupe keys
        self.games = []                          # collected this run
        self.examined = 0                        # parsed games considered (fresh + dup)
        self._brawler_ids = None

    # --- helpers ---
    def _now(self):
        return datetime.now(timezone.utc)

    def brawler_ids(self):
        if self._brawler_ids is None:
            data = self.api.brawlers() or {}
            self._brawler_ids = [b["id"] for b in data.get("items", [])] or []
        return self._brawler_ids

    def _cooldown_ok(self, tag):
        rec = self.producers.get(tag)
        if not rec or not rec.get("lastFetchedAt"):
            return True
        try:
            last = datetime.fromisoformat(rec["lastFetchedAt"])
        except ValueError:
            return True
        return (self._now() - last).total_seconds() > config.REFETCH_COOLDOWN_HOURS * 3600

    # --- elite pool harvest (per-brawler leaderboards + top clubs) ---
    def harvest_elite(self, leaderboard_fetches=30, club_fetches=15):
        ids = self.brawler_ids()
        if not ids:
            return
        countries = config.HARVEST_COUNTRIES
        cursor = self.elite["cursor"]
        for step in range(leaderboard_fetches):
            coord = cursor + step
            country = countries[coord % len(countries)]
            bid = ids[(coord // len(countries)) % len(ids)]
            r = self.api.top_brawler_players(bid, country)
            if r:
                for it in r.get("items", [])[:100]:
                    self.elite["tags"].add(it["tag"])
        country = countries[cursor % len(countries)]
        clubs = self.api.top_clubs(country)
        if clubs:
            for c in clubs.get("items", []):
                self.elite["clubs"].add(c["tag"])
            for c in clubs.get("items", [])[:club_fetches]:
                detail = self.api.club(c["tag"])
                if detail:
                    for m in detail.get("members", []):
                        if m.get("trophies", 0) >= config.ELITE_MEMBER_TROPHY_FLOOR:
                            self.elite["tags"].add(m["tag"])
        self.elite["cursor"] = cursor + leaderboard_fetches + 1

    # --- seeding ---
    def _high_rank_producers(self, limit):
        ranked = sorted(
            (t for t, r in self.producers.items()
             if r.get("bestRankStageSeen", 0) >= config.HIGH_STAGE_FLOOR),
            key=lambda t: self.producers[t].get("highRankGamesSeen", 0), reverse=True)
        return ranked[:limit]

    def _due_producers(self, limit):
        due = [t for t, r in self.producers.items()
               if r.get("timesFetched", 0) > 0 and self._cooldown_ok(t)]

        def yield_score(t):
            r = self.producers[t]
            total = max(r.get("totalGamesSeen", 0), 1)
            ratio = r.get("rankedGamesSeen", 0) / total
            return r.get("freshGamesLastFetch", 0) * (1 + 2 * ratio)
        due.sort(key=yield_score, reverse=True)
        return due[:limit]

    def build_seed_queue(self):
        seeds = []
        seeds += self._high_rank_producers(400)
        elite = [t for t in self.elite["tags"] if self._cooldown_ok(t)]
        random.shuffle(elite)
        seeds += elite[:1500]
        seeds += self._due_producers(400)
        top = self.api.top_players() or {}
        seeds += [it["tag"] for it in top.get("items", [])[:150]]
        # de-dup preserving order
        out, seen = [], set()
        for t in seeds:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    # --- the crawl ---
    def run(self, deadline):
        queue = self.build_seed_queue()
        priority = []                 # high-rank leads drain first
        visited = set()
        club_expansions = 0
        club_budget = 400
        expanded_clubs = set()
        executor = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENCY)

        def note_players(g):
            flat = [b for team in g["teams"] for b in team]
            high = g["rank_stage"] >= config.HIGH_STAGE_FLOOR
            for i, tag in enumerate(g["tags"]):
                if i >= len(flat):
                    break
                rec = self.producers.setdefault(tag, {})
                rec["totalGamesSeen"] = rec.get("totalGamesSeen", 0) + 1
                if g["is_ranked"]:
                    rec["rankedGamesSeen"] = rec.get("rankedGamesSeen", 0) + 1
                rec["bestRankStageSeen"] = max(rec.get("bestRankStageSeen", 0), g["rank_stage"])
                if high:
                    rec["highRankGamesSeen"] = rec.get("highRankGamesSeen", 0) + 1

        while (queue or priority) and time.monotonic() < deadline:
            batch = []
            while (priority or queue) and len(batch) < config.MAX_CONCURRENCY:
                tag = priority.pop(0) if priority else queue.pop(0)
                if tag in visited:
                    continue
                visited.add(tag)
                if not self._cooldown_ok(tag):
                    continue
                batch.append(tag)
            if not batch:
                continue

            results = list(executor.map(lambda t: (t, self.api.battlelog(t)), batch))
            for tag, log in results:
                if log is None:
                    continue
                parsed = parse_battlelog(log, tag)
                fresh = 0
                log_had_ranked = False
                for g in parsed:
                    self.examined += 1
                    if g["dedupe"] in self.seen:
                        continue
                    self.seen.add(g["dedupe"])
                    self.games.append(g)
                    fresh += 1
                    note_players(g)
                    if g["is_ranked"]:
                        log_had_ranked = True
                    if g["rank_stage"] >= config.HIGH_STAGE_FLOOR:
                        priority[:0] = [t for t in g["tags"] if t not in visited]
                # snowball ranked co-players
                if participant_tags(log):
                    queue[:0] = [t for t in participant_tags(log) if t not in visited]
                rec = self.producers.setdefault(tag, {})
                rec["lastFetchedAt"] = self._now().isoformat()
                rec["timesFetched"] = rec.get("timesFetched", 0) + 1
                rec["freshGamesLastFetch"] = fresh

                # recursive elite-club expansion
                if log_had_ranked and club_expansions < club_budget:
                    club_tag = rec.get("clubTag")
                    if not rec.get("clubResolved"):
                        prof = self.api.player(tag)
                        club_tag = (prof or {}).get("club", {}).get("tag")
                        rec["clubTag"] = club_tag
                        rec["clubResolved"] = True
                    if club_tag and club_tag not in expanded_clubs and self._club_due(club_tag):
                        expanded_clubs.add(club_tag)
                        detail = self.api.club(club_tag)
                        if detail:
                            self.elite["clubExpandedAt"][club_tag] = self._now().isoformat()
                            if club_tag in self.elite["clubs"] or detail.get("trophies", 0) >= config.ELITE_CLUB_FLOOR:
                                self.elite["clubs"].add(club_tag)
                                club_expansions += 1
                                members = [m["tag"] for m in detail.get("members", [])
                                           if m["tag"] not in visited]
                                priority[:0] = members
                                self.elite["tags"].update(members)
        executor.shutdown(wait=False)

    def _club_due(self, club_tag):
        iso = self.elite["clubExpandedAt"].get(club_tag)
        if not iso:
            return True
        try:
            last = datetime.fromisoformat(iso)
        except ValueError:
            return True
        return (self._now() - last).total_seconds() > config.CLUB_RECHECK_COOLDOWN_HOURS * 3600
