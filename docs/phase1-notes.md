# Phase 1 design notes

Decisions and constraints to carry into the remaining Phase 1 tasks.

## P1.1-6 Fixtures (football-data.org) (done)

Code: `py/src/fairline/ingest/football_data_org/`, plus the shared name/event resolver
`ingest/resolve.py`. Token: `FOOTBALL_DATA_ORG_TOKEN` in `.env` (free registration).

```
fairline fixtures season [--competition ENG1] [--season 2026-27]   # one request per competition
fairline fixtures window                                            # yesterday..tomorrow
fairline odds aliases --provider football_data_org                  # their unmapped names
fairline odds alias --provider football_data_org add "X FC" "X"
```
Dagster: `fdo_season_daily` (05:15 London) and `fdo_window_15m`, both shipped stopped.

**Coverage.**
- The free tier (10 requests/minute, no monthly cap) covers ENG1, ENG2, ESP1, GER1, FRA1 and
  UCL. It does not cover ESP2, GER2 or FRA2.
- Fixtures for those three come from The Odds API's free `/events` list instead.
  `ingest.the_odds_api.fixture_competitions` lists all 8 leagues for that free sync, while paid
  odds polls stay on `competitions`, the top 4.

**What it adds over the other sources.**
- The full season's fixture list. The Odds API only lists about the next two weeks.
- Kickoff changes, postponements (`postponed`) and cancellations (`void`). The tracker already
  settles a leg on a void event as void.
- Results within one 15-minute window of full time. football-data.co.uk's CSV lags by a day or
  two, so tracked bets now settle the same evening.

**Rules.**
- Football-data.org is authoritative for kickoff and status. It may move a postponed fixture's
  date. A finished event only changes status if it is cancelled.
- `SCHEDULED` (date known, time not) sets `kickoff_time_known = false`. `TIMED` sets it true.
- Results settle on 90 minutes. When a tie goes to extra time, `score.regularTime` is used,
  never `fullTime`.
- Its results are stored with source `football_data_org`. It never overwrites a
  football-data.co.uk result, which carries match stats. When the two scores differ, the sync
  reports it rather than resolving it. A later football-data.co.uk load supersedes its rows and
  links to its events instead of duplicating them.
- Team names go through the same conservative matcher as The Odds API. It tries their short
  name ("Man United") before the full name ("Manchester United FC").

**First live run (2026-11-27, The Odds API, free calls only).**
- The free sync found 123 upcoming fixtures across the 8 leagues.
- 83 names auto-matched, all checked by eye. Reserve sides resolved correctly ("Real Sociedad B"
  -> "Sociedad B", not "Sociedad").
- 13 names were mapped by hand:
  - Atlético -> Ath Madrid
  - Espanyol -> Espanol
  - Paris FC and PSG (each blocked the other from auto-matching, as designed)
  - M'gladbach
  - Ein Frankfurt
  - Hamburg
  - QPR
  - Wolves
  - Celta Fortuna -> Celta B (the reserve side, not Celta)
  - Sp Gijon
  - Karlsruhe
  - Laval
- `fairline odds budget` on the real fixture list: 112 credits for the next 7 days (EPL midweek
  round), about 486 a month, inside the 500 credits.

- **Tracker refresh is chained, not scheduled** (`orchestration/definitions.py`). It runs as the
  last op of `fdo_window_job` (every 15 min) and `fdo_season_job` (05:15), so a failed sync skips
  it. A run-status sensor, `tracker_after_fdcuk`, runs it after each successful fdcuk run, since
  fdcuk is the only result source for ESP2/GER2/FRA2. It also runs inside the odds tick whenever
  that tick finalized closes, so CLV fills minutes after kickoff. It is free and idempotent, and a
  pg advisory lock keeps two runs from overlapping.
- **Settlement rules.**
  - Accas fill CLV per leg as each match closes. They stay open, and count toward open exposure,
    until a leg loses (settled lost then, as the bookmaker does) or every leg is won or void.
  - A leg is void when the match is CANCELLED. It is also void when the match is POSTPONED and not
    played within `tracker.postponed_void_hours` (48) of the kickoff it was placed against, or is
    rearranged to a date past that window.
  - `tracked_bet_leg.kickoff_at` (migration 0007) keeps that original kickoff, because
    football-data.org moves `event.starts_at`. Voiding releases the stake from exposure.
  - House rules differ, so check your books' rules.

## P1.1-7 Live odds poller (The Odds API) (done)

Code: `py/src/fairline/ingest/the_odds_api/`. Key: `THE_ODDS_API_KEY` in `.env`.

```
fairline odds sync [--force]       # free fixture sync: map names, create events, 0 credits
fairline odds plan                 # what the next tick would poll and why (free calls only)
fairline odds budget               # credits this month + 7-day estimate from the free fixture list
fairline odds poll [--dry-run] [--force ENG1]
fairline odds aliases              # names waiting for review / auto-matches to eyeball
fairline odds alias add "Wolverhampton Wanderers" Wolves [--new]
fairline odds alias ok "Manchester United"
```
Dagster: `odds_tick_schedule` every 5 minutes, shipped **stopped**. Turning it on is the
decision to spend credits.

**First run, in this order:**
1. Set `THE_ODDS_API_KEY`.
2. `fairline odds sync`: free. It maps names, creates the upcoming fixtures and fills the review
   queue.
3. Map every name in `fairline odds aliases`. A paid poll skips any fixture with an unmapped
   team.
4. `fairline odds budget`.
5. Turn on the schedule.

The tick also re-syncs each league for free whenever its fixture list refreshes (every 6h), so
newly promoted or renamed teams reach the queue days before their first paid poll.

**Scope: the 4 top divisions only** (ENG1, ESP1, GER1, FRA1), simulated at ~390 credits/month.
Add the second tiers, or pay for a bigger tier, only after live CLV holds up in the tracker.

- **Cost.** `/odds` costs markets x regions. We send `bookmakers=` (10 books = 1 region) from
  `Config.provider_books`: Pinnacle, Betfair Exchange, Sky Bet, William Hill and Paddy Power.
  That is 2 credits a call. Bet365 is not carried, so it is never requested; it stays
  historical-only via football-data.co.uk. `/events` is free and cached for 6h.
- **When.** One call returns a whole league, so the planner decides per league. It polls when a
  fixture enters T-24h, T-3h (the news re-score) or the close window (T-20m). Two rules let one
  poll serve several jobs:
  - A window counts as covered by a poll up to a third of its length early.
  - A window poll waits for a closing poll due within that tolerance.

  Simulated typical weekend, two kickoffs 2.5h apart: 4 polls instead of 6.
- **Budget, checked before every paid call.**
  - Every poll, closing ones included, must keep the API's `x-requests-remaining` above
    `reserve_credits` and our own monthly ledger under the cap.
  - Window polls (T-24h/T-3h) also face two softer rules:
    - **Burst-aware pace.** Spend may run up to `burst_days` (7) of allowance ahead of an even
      monthly line, because fixtures cluster on weekends. A strictly linear line with a 10% slack
      throttled window polls on Sunday 1 Feb 2026 with only the top 4 leagues; the burst rule
      does not. Tests cover Saturday 1 Nov 2025 and Sunday 1 Feb 2026.
    - **Close reserve.** After the poll there must still be room for the month's remaining
      closing polls, projected from the last 7 days' close spend per day x days left. Late in a
      busy month, window polls stop first and closing prices keep flowing.
  - Every paid call is an `ingestion_run` row holding its kind, credits, the remaining balance,
    the poll time and which books priced which event.
- **Honest numbers.**
  - Simulated typical weekends: top 4 divisions ~90 credits (~390/month), all 8 leagues ~168
    (~730/month).
  - With all 8 on the free tier, the rules above keep the closes and drop most window polls.
  - Weeks with midweek rounds cost more than these weekend-only figures. `fairline odds budget`
    estimates the next 7 days from the real fixture list.
- **Timestamps.**
  - A snapshot's `ts` is the market's `last_update`, capped at the poll time, with
    `ts_estimated = false`.
  - Anything stamped at or after kickoff is dropped. `commenceTimeFrom=now` keeps in-play events
    out of the response anyway.
  - The in-play check runs before the clamp, so a skewed stamp cannot sneak through. A test caught
    this.
- **Closing.**
  - After kickoff, each book's latest price from a poll made within 30 minutes of kickoff is copied
    as `is_closing`, with `ts` set to that poll time.
  - Books missing from that poll get no close.
  - An event with no poll near kickoff gets no close at all: a T-3h price is not a close.
- **Names and events.**
  - football-data.co.uk spellings stay canonical.
  - A provider name auto-matches only on an exact normalised match, or a unique token-subset match
    against teams from the country's last three seasons. Normalising handles Man/Manchester,
    Nott'm, St/Saint, accents and FC/AFC.
  - Two names never share a team.
  - Everything else goes to `participant_alias_review`, and that fixture is skipped. A wrong match
    would put prices on the wrong team, which is worse than a missing match.
  - League fixtures match on (season, home, away). football-data.co.uk's loader now links to an
    event the poller created instead of duplicating it.
- **Secrets.** httpx request logging is silenced, since it prints the URL and so the `apiKey`.
  Errors are rebuilt without the URL, and cache metadata omits the key. Tests check all three.
- **Derived O/U 2.5 reference.**
  - *The gap.* On the first live poll, Pinnacle quoted only its main totals line, which was 2.5 for
    just 11 of 78 matches (otherwise 2.75/3.0/3.25...). William Hill quoted 2.5 everywhere. Sky Bet,
    Paddy Power and Betfair returned no totals at all.
  - *Derivation* (`models/football/implied.py`): fit a Dixon-Coles matrix to Pinnacle's devigged
    1X2 and devigged main line. An outer brentq on total goals matches the Asian-settled effective
    probability of Over; an inner brentq keeps the home/away split on the 1X2 ratio; rho is the
    league's latest fit. The round trip from known lambdas is exact.
  - *Storage.* Results go to the `pinnacle_derived` pseudo-bookmaker (kind `derived`), and
    `odds_derivation` records the source line, prices, lambdas, rho and the draw-fit error. The
    board shows "derived from 3.0". A quoted 2.5 in the same poll always wins.
  - *What rho really does* (measured):
    - tau only moves mass among 0-0, 1-0, 0-1 and 1-1, all totals <= 2, and preserves it. So for
      main lines >= 2.75 the derived 2.5 equals a plain-Poisson fit exactly.
    - It changes the answer only for main lines <= 2.25, by 0.2-0.3pp (0.5-0.8% of the price).
    - The home/away anchoring matters only through tau, since a sum of independent Poissons depends
      on the total alone.
  - *Like-for-like CLV.* A quoted placement reference only compares with a quoted close; a derived
    one only with a close derived from the same source line. Anything else leaves CLV empty with a
    `clv_note`, so a 0.5-1% line-translation residual cannot pass for CLV. The backtester never
    loads derived books; historical Pinnacle O/U is always quoted.
  - *Closes copy only lines the close poll priced* (fixed after the first live evening, 27 Nov).
    Pinnacle quotes one totals line per match (451 of 451 cached markets). Forest v Chelsea's main
    line moved from 2.5 to 2.75 after 17:00. `finalize_closing` used to copy each book's latest
    row per market, so the 17:00 quoted 2.5 was stamped as a 19:40 close beside the derived one. A
    test bet then showed CLV against a 2h40m-old price. Each poll now records `quoted`
    (book|market|line keys, derived included), and only those keys are copied. Older polls rebuild
    the keys from their cached response. `fairline odds close --redo-since DATE` rebuilds closes
    and clears tracked CLV, so the next refresh recomputes it.
  - *Accuracy of derived vs quoted 2.5.* This can't be measured at the same instant. With one
    Pinnacle line per match, a poll has either a quoted 2.5 or a derived one, never both, and
    football-data.co.uk history has only the 2.5 line. The check that will work is outcome
    calibration: log loss of derived vs quoted references on settled matches, once a few hundred
    have built up.
- **Not done here.**
  - Live predictions for upcoming fixtures (the model only scores past matchdays so far), and so a
    live board.
  - football-data.org fixtures (P1.1-6).
  - Telegram budget alerts (Phase 2); today it logs a warning at 80%.
  - Verifying the bookmaker keys and names against the real API, which needs the key.

## P1.2 Model (done)

- Cold start: `pooled` (top and second tier fitted together per country, each team shrunk
  towards its own division's mean) is the default. In a walk-forward from 2016-17, it matched
  or beat the bottom-quartile `prior` mode on promoted clubs' first 10 matches in ESP, GER
  and FRA. ENG promoted clubs did better under `prior` (0.924 vs 0.946 log loss).
  Re-check with confidence intervals in P1.4.
- `prediction_rate` stores lambda_home, lambda_away, rho and max_goals per match per fit, so
  BTTS, Asian handicap and other lines can be recomputed for any historical fold without
  refitting (`models/football/recompute.py`).
- PyMC divergences: about 1 per 4,000 draws on some German slates. They are not fixed by
  target_accept 0.95, and the hierarchy is already non-centred. At least one divergent
  draw sits where a strongly negative rho pushes the low-score correction towards 0.
  `BayesResult.healthy` flags any slate with r-hat > 1.01 or a divergence rate > 0.5%.

## P1.3 Edge engine (done)

- Devigging works on one complete snapshot per bookmaker (every outcome priced, same
  timestamp). Partial or mixed-timestamp markets are skipped and counted. If sum(1/O) <= 1
  the market is flagged `no_margin` and never passed to the Shin/Power root-finders, and the
  next reference book is used. In 2-way markets Shin equals additive (tested).
- Pinnacle is reference-only (it doesn't take UK customers); `bettable_books` lists where
  you can bet. Betfair commission applies when Betfair is the betting venue, not when it is
  the reference: commission changes the payout, not the implied probability.
- Kelly maximises expected log-wealth over win / half-win / push / half-loss / loss, so
  Asian lines are sized correctly. Binary bets reduce to the closed form.
- Caps run per bet (2%), then per match (2%, all markets), then per slate (10%). A binding
  cap scales its whole group proportionally. Open tracked bets use up the slate cap.
- Accas: `max_odds` applies per leg and the combined price has its own cap
  (`acca_max_combined_odds`). Same-match legs get an exact joint probability from the score
  matrix and need a bet-builder price before EV is shown. Asian legs are refused for now.
- First evidence (ENG 2024-25, decided at kickoff - 2h, flat stakes, one season):
  - 836 board picks: model EV +16.6%, EV vs devigged Pinnacle -3.7%, realised ROI -2.9%.
    The realised result tracks the market's view, not the model's.
  - Picks that also beat the sharp price (ev_vs_ref >= 0): 54 bets, ROI +11.5%. The sample
    is far too small to mean anything yet.
  - The `model_disagrees_with_market` flag: 153 bets, ROI -19.3%. Large disagreements are
    mostly model misses.
  - The slate cap bound on 553 of 836 picks.
  P1.4 should test filters (require ev_vs_ref >= 0, hide disagreement-flagged picks)
  walk-forward, with confidence intervals, before any default changes.

## P1.4 Backtester (done)

`fairline backtest run` (stored as `backtest_run` rows) and `fairline backtest bands`.

Method:
- Each match is decided at kickoff - 2h, using walk-forward predictions with as_of <= decision
  and prices with ts <= decision. Closing prices only score bets (CLV), never choose them.
  football-data.co.uk pre-match prices carry an estimated ts of kickoff - 2h; they were really
  collected earlier (Friday or Tuesday), so with this data source only offsets <= 2h mean anything.
- Five strategies share the same evaluated prices, selection rules (min_ev 3%, max_odds 6,
  line shopping, one pick per match) and staking caps, on one bankroll across all pools:
  model, model_beats_ref, model_no_disagree, reference (pure soft-book hunting, model ignored)
  and blend. The blend weight for season S is fitted on log loss over seasons before S only.
- Confidence intervals come from a cluster bootstrap over matchdays, since bets on the same day
  are correlated. Log-loss differences against the Pinnacle close are paired on the same matches.

Results (ENG, ESP, GER, FRA, 2017-18 to 2025-26, backtest_run id 1):

| Strategy | Bets | Flat ROI (95% CI) | CLV (95% CI) | Kelly bankroll | Max drawdown | Slate cap binds |
|---|---|---|---|---|---|---|
| model | 22,458 | -8.3% [-10.1, -6.5] | -5.0% [-5.1, -4.9] | 0.00x | 100% | 86% |
| model_beats_ref | 1,423 | -3.5% [-11.3, +4.8] | +0.9% [+0.3, +1.4] | 0.26x | 81% | 1% |
| model_no_disagree | 20,383 | -7.8% [-9.8, -5.9] | -5.1% | 0.00x | 100% | 83% |
| reference | 777 | -2.2% [-13.3, +8.4] | +2.3% [+1.5, +3.1] | 0.93x | 32% | 0% |
| blend | 783 | -2.6% [-13.4, +8.6] | +2.2% | 0.93x | 32% | 0% |

- **The model adds no signal over the sharp price.** The fitted blend weight on Dixon-Coles
  is 0 (at most 0.05) in every pool and season. 1X2 log loss: model +0.0197 vs close,
  Pinnacle at decision time +0.0028, a CI of [+0.0174, +0.0220] for the model.
- **Kelly on the raw model is ruinous.** Overconfident probabilities plus Kelly drove the
  bankroll to zero. The slate cap binds on 86% of picks, which compresses stake differences
  between strong and weak picks. Once the "must beat Pinnacle" filter is applied it binds on
  only 1%.
- **Only the sharp-price strategies beat the close:** reference CLV +2.3%, 61.5% of bets beat
  the close. ROI CIs are too wide to call (777 bets at average odds of 3.5); CLV is the better
  evidence. Caveat: these are price-collection-time quotes from three soft books (B365, WH,
  Betfair); real availability, limits and stale prices are untested.
- **Calibration:** 1X2 is close to calibrated. O/U 2.5 is badly overconfident at the extremes
  (predicted 0.18 -> realised 0.30, 0.82 -> 0.70), which points to too little shrinkage of team
  strengths or too short a half-life.
- **PyMC bands are too wide:** on 24 slates the Pinnacle close fell inside the 90% band 98.7%
  of the time (mean width 0.29), so conservative EV built on them is overly pessimistic.
  The binned realised rates all sat inside the bands, which is uninformative at this width.
  Heavy favourites (mean p 0.71) realised 0.61.

Next steps this points to:
1. Tune `half_life_days` and `prior_sd` walk-forward against the O/U and 1X2 log loss,
   choosing on early seasons and confirming on later ones.
2. Treat the model as an adjustment to the sharp price (e.g. news effects), not a
   replacement. The default board should probably require `ev_vs_ref >= 0`.
3. Re-check band width after tuning; the time-decay weights probably inflate the
   posterior variance.

## P1.5 API and dashboard (done)

Run: `make api` (FastAPI on :8000) and `make web` (Next.js on :3100; `/api/*` is proxied to the API).

Built around P1.4's result: only soft-book prices that beat devigged Pinnacle beat the close.
So the dashboard's job is to verify live CLV, not to surface raw model EV.

- **API** (`py/src/fairline/api/`): thin. Routes call `edge.board.evaluate/select`,
  `edge.kelly.size`, `edge.acca.build` and `tracker.service` directly. Pydantic schemas mirror
  the domain dataclasses.
- **Value board** (`/`): defaults to "Must beat Pinnacle". Model EV and Pinnacle EV sit side by
  side. Probabilities show PyMC bands when "Uncertainty bands" is on (about 5-10 s per slate,
  cached in-process), along with an effective-sample badge, a `Model ≠ Market` badge, and
  cold-start and capped-stake markers. "Log bet" pre-fills the tracker with the book, odds,
  suggested stake and decision time.
- **Event page** (`/events/[id]`): Lightweight Charts loaded via `next/dynamic({ ssr: false })`.
  Pinnacle is the bold line and the bettable books are thin ones. Every football-data.co.uk
  point has an estimated timestamp and is drawn dashed.
- **Backtests** (`/backtests/[id]`): CLV with its bootstrap CI is the headline and ROI comes
  second. 1X2 and O/U 2.5 calibration plots sit side by side.
- **Tracker** (`/tracker`): each leg stores the devigged sharp price at placement
  (`execution_edge`: real edge, or a stale line?) and the devigged close (`clv`). Results and
  CLV fill in on refresh. The headline is CLV with a matchday-cluster bootstrap CI.
- **Acca builder** (`/acca`): legs come from the board. Same-match groups get exact joint
  probabilities and a field for the bet-builder price.

Not yet live: every price is still historical football-data.co.uk data. Live boards need the
Odds API poller (P1.1-7) and the football-data.org fixtures (P1.1-6). Until those exist, the
tracker works on historical matches, with placement time pre-filled as the decision time.
