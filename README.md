# BS Buddy Meta Collector

A tiny server-side collector for the BS Buddy app. A GitHub Actions cron runs
it every 30 minutes; each run crawls high-rank Brawl Stars battle logs, folds
them into an aggregated meta snapshot, ages old data with a one-week half-life,
and commits the fresh `data/snapshot.json` back to this repo. The app just does
one GET on that file's raw URL. Nobody's phone collects; the data is always
fresh and shared.

## What it does each run
1. Ages the existing snapshot: one-week half-life over elapsed time, plus a hard
   decay across any balance-patch date crossed (so brawlers aren't judged on
   pre-patch data).
2. Crawls under a time budget (default 20 min), seeded from the elite pool
   (per-brawler leaderboards + top clubs), high-rank producers, and the
   producer roster; snowballs through ranked co-players; recursively expands
   elite clubs. High-rank (Legendary+) leads are chased first.
3. Aggregates new games into per-map / per-brawler tallies (ranked x2, ladder
   x1; tournament data is added by the app), with raw ranked counts split by
   rank bucket, plus versus/synergy pairs.
4. Commits updated `data/` (snapshot + producer roster + elite pool + dedupe)
   so the next run continues where this one left off.

## The snapshot the app downloads
`data/snapshot.json`, public raw URL:
`https://raw.githubusercontent.com/<you>/<repo>/main/data/snapshot.json`
It contains only aggregate meta (win rates / tallies) - no personal data, no
secrets. Public is fine.

## SETUP (the 3 steps only you can do)
1. Create a new PUBLIC GitHub repo (public = unlimited free Actions minutes),
   e.g. `bsbuddy-meta-collector`, and push this folder to it:
   ```
   git remote add origin https://github.com/<you>/bsbuddy-meta-collector.git
   git push -u origin main
   ```
2. In the repo: Settings -> Secrets and variables -> Actions -> New repository
   secret. Name it `BS_PROXY_KEY`, value = your RoyaleAPI proxy key. (This is
   the same key the app uses. It is stored encrypted and never appears in the
   code or the snapshot.)
3. In the repo: Actions tab -> enable workflows if prompted. Then open the
   "collect-meta" workflow -> Run workflow (manual first run), or just wait for
   the 30-minute cron. Watch the run log for `new_games=... high_rank=...`.

## Tuning
- Collect more per run: raise `BS_TIME_BUDGET` (workflow input) and/or the cron
  frequency in `.github/workflows/collect.yml`. Keep `BS_RPS` polite (default 8)
  so the shared proxy never bans the key.
- Balance patches: add the date to `PATCH_DATES` in `collector/config.py` (and
  optionally the affected brawler ids to `PATCH_AFFECTED_BRAWLERS`).
- Half-life: `HALF_LIFE_DAYS` (default 7).

## Local test (optional, needs the key)
```
pip install -r requirements.txt
BS_PROXY_KEY=... BS_TIME_BUDGET=120 python -m collector.main
```

## Honest limits
- A million *high-rank* games/day is not attainable: Legendary+ is <1% of
  players and Masters+ is a few tens of thousands of accounts, so that many
  distinct high-rank games are not played daily. This collector maximizes
  distinct high-rank games under a polite request budget; ~tens of thousands of
  targeted high-rank games/day accumulated with the one-week half-life is
  plenty for a trustworthy tier list. Do not crank the rate to chase a million;
  it mostly harvests low-rank noise and risks a proxy ban.
