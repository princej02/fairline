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
