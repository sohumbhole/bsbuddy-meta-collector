"""The crawl engine: seed from the elite pool + high-rank producers, snowball
through ranked co-players, and expand elite clubs recursively. Ports the app's
MetaService strategy to run server-side under a time budget.
"""
import time
import random
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
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
        # Rank buckets that are thin in the dataset (P1.4): when we ENCOUNTER a
        # player in one of these ranks we dig in (prioritize their co-players)
        # instead of moving on, and we seed known players of these ranks. Set by
        # main() from the snapshot each run. High rank is ALWAYS priority; this
        # is additive so under-served ranks (e.g. diamond) build a baseline too.
        self.wanted_buckets = set()
        # Population discovery via neighbor tag-guessing (Sohum's idea): tags
        # encode a sequential creation ID uncorrelated with skill, so perturbing
        # a known valid tag's trailing chars samples same-era accounts = roughly
        # uniform skill = mostly the LOW-trophy/LOW-rank players snowball + top
        # leaderboards can't reach. This set is the candidates injected this run,
        # for measuring the hit rate afterward.
        self.candidate_tags = set()
        self.candidate_hits = 0        # candidate tags that returned fresh games
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
        hours_since = (self._now() - last).total_seconds() / 3600.0
        # Dormant players (no fresh games in a while, or repeatedly-empty
        # fetches) get a long cooldown so we stop wasting calls on them; active
        # players are fair game every REFETCH_COOLDOWN_HOURS.
        cooldown = config.REFETCH_COOLDOWN_HOURS
        last_fresh = rec.get("lastFreshAt")
        if last_fresh:
            try:
                fresh_ago = (self._now() - datetime.fromisoformat(last_fresh)).total_seconds() / 3600.0
                if fresh_ago > config.DORMANT_AFTER_HOURS:
                    cooldown = config.DORMANT_COOLDOWN_HOURS
            except ValueError:
                pass
        elif rec.get("timesFetched", 0) >= 2:
            # fetched twice, never yielded fresh games -> treat as dormant
            cooldown = config.DORMANT_COOLDOWN_HOURS
        return hours_since > cooldown

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

    def _wanted_bucket_producers(self, limit):
        # Known players whose rank falls in a thin bucket (P1.4), off cooldown.
        if not self.wanted_buckets:
            return []
        out = [t for t, r in self.producers.items()
               if config.rank_bucket(r.get("bestRankStageSeen", 0)) in self.wanted_buckets
               and self._cooldown_ok(t)]
        random.shuffle(out)
        return out[:limit]

    # Brawl Stars tag alphabet (14 chars). Tags encode a sequential account
    # creation id, so this is the full symbol set trailing digits are drawn from.
    _TAG_ALPHABET = "0289PYLQGRJCUV"

    def _candidate_tags(self, n):
        # Sohum's population-discovery idea: perturb the trailing 1-2 chars of a
        # KNOWN valid tag to sample the same account-id neighborhood (same-era
        # accounts). Creation order is uncorrelated with skill, so the valid hits
        # are a roughly uniform skill sample = mostly the LOW-trophy / LOW-rank
        # players the snowball + top leaderboards can't reach. Invalid guesses
        # just 404 (cheap: we're supply-limited, not rate-limited, so idle
        # capacity pays for the misses). Valid ones enter the normal battlelog
        # path and their games/co-players get harvested like any producer.
        pool = list(self.producers.keys()) + list(self.elite["tags"])
        if not pool:
            return []
        out = set()
        tries = 0
        while len(out) < n and tries < n * 5:
            tries += 1
            core = random.choice(pool).lstrip("#")
            if len(core) < 5:
                continue
            k = random.choice([1, 1, 2])   # bias to 1 char = stay closer/denser
            cand = "#" + core[:-k] + "".join(random.choice(self._TAG_ALPHABET)
                                             for _ in range(k))
            if cand[1:] == core or cand in self.producers:
                continue
            out.add(cand)
        self.candidate_tags |= out
        return list(out)

    def build_seed_queue(self):
        seeds = []
        # Thin-rank players first, so under-served ranks build a baseline even
        # while high-rank stays the priority (it jumps the priority queue below).
        seeds += self._wanted_bucket_producers(600)
        seeds += self._high_rank_producers(400)
        elite = [t for t in self.elite["tags"] if self._cooldown_ok(t)]
        random.shuffle(elite)
        seeds += elite[:1500]
        seeds += self._due_producers(400)
        top = self.api.top_players() or {}
        seeds += [it["tag"] for it in top.get("items", [])[:150]]
        # Population-discovery guesses LAST: they only get fetched once the known
        # queue drains (we're supply-limited, so that idle tail is exactly what
        # pays for the 404s). More when a bucket is thin, since guesses skew
        # low-rank = the underserved brackets. Discovered valid players persist
        # in `producers` and get re-checked on the normal cooldown thereafter, so
        # this snowballs a standing low-bracket roster over successive runs.
        seeds += self._candidate_tags(1200 if self.wanted_buckets else 500)
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

        # Process ONE fetched battlelog. Runs on the main thread only (never in
        # a worker), so all the shared-state mutation below (seen/games/
        # producers/queue/priority/elite) stays single-threaded and lock-free,
        # exactly as before. Returns nothing; mutates state + enqueues snowball
        # tags. `nonlocal` for the club-expansion budget counter.
        def process(tag, log):
            nonlocal club_expansions
            if log is None:
                return
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
                # Dig deeper on high-rank AND on thin-rank games (P1.4): jump
                # their co-players to the front so we build those ranks up.
                if g["rank_stage"] >= config.HIGH_STAGE_FLOOR or \
                   (g["is_ranked"] and g.get("rank_bucket") in self.wanted_buckets):
                    priority[:0] = [t for t in g["tags"] if t not in visited]
            # snowball ranked co-players
            if participant_tags(log):
                queue[:0] = [t for t in participant_tags(log) if t not in visited]
            rec = self.producers.setdefault(tag, {})
            rec["lastFetchedAt"] = self._now().isoformat()
            rec["timesFetched"] = rec.get("timesFetched", 0) + 1
            rec["freshGamesLastFetch"] = fresh
            if fresh > 0:
                rec["lastFreshAt"] = self._now().isoformat()  # dormancy signal
                if tag in self.candidate_tags:
                    self.candidate_hits += 1   # guessed tag that turned up real games

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

        # Continuous worker pool: keep MAX_CONCURRENCY battlelog fetches in
        # flight at all times instead of fetching a batch of N then blocking on
        # the slowest of the N (the old executor.map pattern capped effective
        # throughput at ~4 req/s even with 0 proxy throttling; the real ceiling
        # is now the RateLimiter's TARGET_RPS). Snowball tags discovered while
        # processing immediately top the pool back up.
        inflight = {}   # future -> tag

        def refill():
            while len(inflight) < config.MAX_CONCURRENCY and (priority or queue):
                tag = priority.pop(0) if priority else queue.pop(0)
                if tag in visited:
                    continue
                visited.add(tag)
                if not self._cooldown_ok(tag):
                    continue
                inflight[executor.submit(self.api.battlelog, tag)] = tag

        refill()
        while inflight and time.monotonic() < deadline:
            done, _ = wait(list(inflight), timeout=2.0, return_when=FIRST_COMPLETED)
            for fut in done:
                tag = inflight.pop(fut)
                try:
                    log = fut.result()
                except Exception:
                    log = None
                process(tag, log)
            refill()
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
