# Phase 1 design notes

Decisions and constraints to carry into the remaining Phase 1 tasks.

## P1.1-6 Fixtures (football-data.org)

- The free tier (10 requests/minute) covers ENG1, ENG2 (Championship), ESP1, GER1, FRA1 and UCL,
  but **not** Segunda, 2. Bundesliga or Ligue 2. `competitions.yaml` already has
  `football_data_org: null` for those three.
- Upcoming fixtures for ESP2, GER2 and FRA2 should come from The Odds API events list,
  which we poll for odds anyway. That avoids a paid tier. Resolve team names through
  `participant_alias` as usual.

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

## P1.4 Backtester

- Validate the PyMC 90% bands with binned calibration, not by checking whether outcomes fall
  inside them. An interval on p can't "contain" a 0/1 outcome. Bin predictions by
  posterior mean (or by band) and check that the realised win rate in each bin falls inside
  the bin's band. Or use posterior predictive calibration (PIT-style checks).
- Report log loss and Brier with bootstrap confidence intervals, and always alongside the
  Pinnacle-close baseline with the margin removed. In P1.2 the MLE was about 0.02 worse
  than that baseline on 1X2 log loss in every country.
- Bet only at prices whose `ts` is at or before the decision time. Pre-match
  football-data.co.uk prices carry a conservative estimated ts (kickoff - 2h).
