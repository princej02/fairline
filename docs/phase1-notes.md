# Phase 1 design notes

Decisions and constraints to carry into the remaining Phase 1 tasks.

## P1.1-6 Fixtures (football-data.org)

- The free tier (10 requests/minute) covers ENG1, ENG2 (Championship), ESP1, GER1, FRA1 and UCL,
  but **not** Segunda, 2. Bundesliga or Ligue 2. `competitions.yaml` already has
  `football_data_org: null` for those three.
- Upcoming fixtures for ESP2, GER2 and FRA2 should come from The Odds API events list,
  which we poll for odds anyway. That avoids a paid tier. Resolve team names through
  `participant_alias` as usual.

## P1.1-7 Live odds (The Odds API) - notes for when it is built

- Billing: each `/odds` call costs `markets x regions` credits; `/events` and `/sports` are free.
  Pinnacle is only in the `eu` region, so `uk,eu` x `h2h,totals` = 4 credits per league per poll.
  Use `bookmakers=` instead (every 10 books bill as one region): 2 credits. Build the list from
  `Config.provider_books("the_odds_api")`: Pinnacle, Betfair Exchange, Sky Bet, William Hill and
  Paddy Power. Bet365 is not carried by The Odds API, so it is left out (no wasted slot, no
  missing-row alarms). It stays historical-only via football-data.co.uk, so live Bet365 prices
  can't appear on the board; it stays bettable and you can still log Bet365 bets by hand. A paid UK
  aggregator (Odds-API.io, TheStatsAPI) would carry Bet365 and Pinnacle in one feed if that's ever
  worth paying for. Re-check the remaining keys against `/sports/{sport}/odds` on the first live run.
- Budget: 500 credits/month over 8 leagues at 2 credits is ~31 polls per league per month. Poll each
  league only on matchdays, once ~T-24h and once ~T-3h (the news re-score), plus a closing grab at
  kickoff; skip leagues with no fixture in the window (from the free `/events` call).
- Log `x-requests-remaining` and `x-requests-used` from every response into `ingestion_run`, refuse
  to poll when the projected month-end spend exceeds `monthly_credit_budget`, and alert (Phase 2
  Telegram) at 80%.
- Cache every response body on disk before parsing, as the football-data.co.uk source does.
- Live snapshots carry a real `ts` (the API's `last_update`), so `ts_estimated = false` and the
  tracker's execution edge stops depending on the kickoff-2h estimate.

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
