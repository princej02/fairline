// Typed client for the Fairline API. Types mirror py/src/fairline/api/schemas.py.

export type StrategyName = "model_beats_ref" | "reference" | "model" | "model_no_disagree";

export interface StrategyInfo {
  name: StrategyName;
  label: string;
  description: string;
}

export interface Settings {
  pools: Record<string, string[]>;
  bettable_books: string[];
  reference_books: string[];
  kelly_fraction: number;
  max_stake_pct: number;
  max_match_exposure_pct: number;
  max_slate_exposure_pct: number;
  min_ev: number;
  max_odds: number;
  devig_method: string;
  bankroll: number;
  currency: string;
  strategies: StrategyInfo[];
  default_strategy: StrategyName;
}

export interface Band {
  lo: number;
  hi: number;
  conservative: number;
  interval: number;
}

export interface BoardRow {
  event_id: number;
  label: string;
  competition: string | null;
  starts_at: string;
  decision_ts: string;
  market: string;
  line: number | null;
  selection: string;
  bookmaker: string;
  odds_ts: string;
  price: number;
  effective_price: number;
  p_model: number;
  p_ref: number | null;
  ref_book: string | null;
  ref_note: string | null; // e.g. "derived from 3.0 (draw fit +0.4%)": computed, not quoted
  p_bet: number;
  ev_strategy: number;
  ev_model: number;
  ev_vs_ref: number | null;
  ev_conservative: number | null;
  band: Band | null;
  home_effective_matches: number | null;
  away_effective_matches: number | null;
  cold_start: boolean;
  stake_pct: number;
  stake_amount: number;
  capped_by: string[];
  flags: string[];
}

export interface Board {
  pool: string;
  date: string;
  strategy: StrategyName;
  bands: boolean;
  rows: BoardRow[];
  evaluated_prices: number;
  skipped_snapshots: Record<string, number>;
  notes: string[];
}

export interface Matchday {
  date: string;
  matches: number;
}

export interface OddsPoint {
  ts: string;
  price: number;
  is_closing: boolean;
  ts_estimated: boolean;
}

export interface OddsSeries {
  bookmaker: string;
  selection: string;
  is_reference: boolean;
  points: OddsPoint[];
}

export interface EventDetail {
  event_id: number;
  label: string;
  competition: string;
  season: string;
  starts_at: string;
  home: string;
  away: string;
  home_goals: number | null;
  away_goals: number | null;
  model: Record<string, number>;
  model_as_of: string | null;
  rates: { lambda_home: number; lambda_away: number; rho: number } | null;
  close_fair: Record<string, number>;
}

export interface AccaLegIn {
  event_id: number;
  market: "1X2" | "OU" | "BTTS";
  selection: string;
  price: number;
  line: number | null;
  bookmaker: string;
  label?: string;
}

export interface AccaOut {
  ok: boolean;
  legs: { event_id: number; market: string; selection: string; line: number | null; price: number; p_model: number; ev: number; drags: boolean }[];
  p_joint: number | null;
  p_naive: number;
  combined_price: number | null;
  ev: number | null;
  kelly_full: number;
  suggested_stake_pct: number;
  compounded_margin: number | null;
  same_match_fair_odds: Record<string, number>;
  warnings: string[];
  blocked: string[];
}

export interface BetLegIn {
  event_id: number;
  market: "1X2" | "OU";
  selection: string;
  price: number;
  line: number | null;
  p_model?: number | null;
}

export interface BetIn {
  legs: BetLegIn[];
  bookmaker: string;
  stake: number;
  placed_at: string;
  strategy?: string | null;
  source: "board" | "manual";
  suggested_stake_pct?: number | null;
  notes?: string | null;
}

export interface BetLeg {
  leg_no: number;
  event_id: number;
  label: string;
  market: string;
  line: number | null;
  selection: string;
  price: number;
  effective_price: number | null;
  ref_book: string | null;
  ref_fair_prob: number | null;
  execution_edge: number | null;
  close_fair_prob: number | null;
  clv: number | null;
  result: string | null;
  ref_source_line: number | null; // placement reference derived from this line (null = quoted)
  close_source_line: number | null;
  clv_note: string | null; // why CLV is empty: no like-for-like close
}

export interface Bet {
  id: number;
  placed_at: string;
  bookmaker: string | null;
  stake: number;
  price: number;
  status: string;
  pnl: number | null;
  strategy: string | null;
  source: string;
  suggested_stake_pct: number | null;
  notes: string | null;
  legs: BetLeg[];
}

export interface CI {
  mean: number;
  lo: number;
  hi: number;
  n: number;
  positive_share: number;
}

export interface TrackerSummary {
  bets: number;
  open: number;
  settled: number;
  staked: number;
  pnl: number;
  roi: number | null;
  clv: CI | null;
  execution_edge: CI | null;
  clv_by_book: Record<string, { n: number; mean: number | null }>;
}

export interface BacktestListItem {
  id: number;
  started_at: string;
  pools: string[];
  first: string;
  last: string;
}

export interface CalibrationBin {
  lo: number;
  hi: number;
  n: number;
  mean_p: number;
  observed: number;
  ci_lo: number;
  ci_hi: number;
}

export interface BettingRow {
  strategy: string;
  bets: number;
  hit: number;
  avg_odds: number;
  mean_ev: number;
  flat_roi: number;
  flat_roi_ci: string;
  clv: number;
  clv_ci: string;
  beat_close: number;
  kelly_roi: number;
  kelly_roi_ci: string;
  bankroll_x: number;
  max_dd: number;
  slate_cap_rate: number;
  avg_stake: number;
}

export interface ProbabilityRow {
  market: string;
  source: string;
  matches: number;
  log_loss: number;
  log_loss_ci: string;
  brier: number;
  minus_close?: string | null;
}

export interface Backtest {
  id: number;
  started_at: string;
  config: { pools: string[]; first: string; last: string; offset_hours: number; [k: string]: unknown };
  metrics: {
    betting: BettingRow[];
    probability: ProbabilityRow[];
    calibration?: Record<string, CalibrationBin[]>;
    per_season?: { strategy: string; season: string; bets: number; flat_roi: number; clv: number | null }[];
    blend_weights?: Record<string, number>;
  };
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { "content-type": "application/json", ...init?.headers } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

const qs = (p: Record<string, string | number | boolean | null | undefined>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => [k, String(v)])).toString();

export const api = {
  settings: () => request<Settings>("/api/settings"),
  matchdays: (pool: string) => request<Matchday[]>(`/api/matchdays?${qs({ pool })}`),
  board: (pool: string, date: string, strategy: StrategyName, bands: boolean) =>
    request<Board>(`/api/board?${qs({ pool, date, strategy, bands })}`),
  event: (id: number) => request<EventDetail>(`/api/events/${id}`),
  odds: (id: number, market: string) => request<OddsSeries[]>(`/api/events/${id}/odds?${qs({ market })}`),
  acca: (legs: AccaLegIn[], builder_prices: Record<number, number>) =>
    request<AccaOut>("/api/acca", { method: "POST", body: JSON.stringify({ legs, builder_prices }) }),
  bets: () => request<Bet[]>("/api/bets"),
  createBet: (body: BetIn) => request<Bet>("/api/bets", { method: "POST", body: JSON.stringify(body) }),
  refreshBets: () => request<{ clv_filled: number; settled: number }>("/api/bets/refresh", { method: "POST" }),
  voidBet: (id: number) => request<Bet>(`/api/bets/${id}/void`, { method: "POST" }),
  trackerSummary: () => request<TrackerSummary>("/api/tracker/summary"),
  backtests: () => request<BacktestListItem[]>("/api/backtests"),
  backtest: (id: number) => request<Backtest>(`/api/backtests/${id}`),
};
