/** Mirrors the FastAPI response shapes — keep in sync with backend/routers/*.py */

export type AuthStatus = {
  authenticated: boolean;
  user_id?: string | null;
  user_name?: string | null;
  email?: string | null;
};

export type ParamSpec = {
  name: string;
  type: "float" | "int" | "str" | "bool";
  default: number | string | boolean | null;
  description: string;
};

export type StrategyInfo = {
  name: string;
  description: string;
  params: ParamSpec[];
};

export type ExecutionMode = "signals" | "paper";

export type RunStatus = "RUNNING" | "STOPPING" | "STOPPED" | "ERRORED";

export type RunSummary = {
  id: string;
  strategy_name: string;
  mode: string;
  params: Record<string, unknown>;
  status: RunStatus;
  created_at: string;
  stopped_at?: string | null;
  last_tick_at?: string | null;
  tick_count: number;
  error?: string | null;
  n_signals: number;
  n_trades: number;
  last_eod_report?: Record<string, unknown> | null;
};

export type ProposalEntry = {
  timestamp: string;
  kind: "ENTRY" | "REHEDGE";
  tradingsymbol: string;
  transaction_type: "BUY" | "SELL";
  quantity: number;
  lot_size: number;
  price: number;
  rationale: string;
  mode: string;
  status: string;
  order_id?: string | null;
};

export type PnlSnapshot = {
  timestamp: string;
  report?: Record<string, unknown>;
  error?: string;
};

export type RunDetail = RunSummary & {
  signals: ProposalEntry[];
  trades: ProposalEntry[];
  pnl_history: PnlSnapshot[];
};

// ── Market Profile ──

export type MarketProfileSymbol = {
  symbol: string;
  instrument_token: number;
  exchange: string;
  name?: string | null;
  last_backfilled_at?: string | null;
  last_update_at?: string | null;
  earliest_bar_ts?: string | null;
  latest_bar_ts?: string | null;
};

export type ProfileBin = {
  price_low: number;
  price_high: number;
  price_mid: number;
  tpo_count: number;
  letters: string;
  in_value_area: boolean;
  is_poc: boolean;
};

export type CompositeProfile = {
  bins: ProfileBin[];
  poc: number;
  vah: number;
  val: number;
  high: number;
  low: number;
  total_tpos: number;
  total_volume: number;
  n_days: number;
  n_periods: number;
};

export type DayProfile = {
  day: string;
  bins: ProfileBin[];
  poc: number;
  vah: number;
  val: number;
  ib_high: number | null;
  ib_low: number | null;
  open: number;
  close: number;
  high: number;
  low: number;
  n_periods: number;
  total_tpos: number;
  total_volume: number;
};

// ── Pair Candidates ──

export type PairCandidate = {
  symbol_a: string;
  symbol_b: string;
  correlation: number;
  hedge_ratio: number;
  coint_pvalue: number;
  half_life_days: number;
  spread_vol_pct: number;
  spread_mean: number;
  spread_std: number;
  latest_spread: number | null;
  latest_z_score: number | null;
  last_close_a: number | null;
  last_close_b: number | null;
  last_data_date: string | null;
  n_obs: number;
  rank_score: number;
};

export type PairCandidatesResponse = {
  generated_at: string | null;
  candidates: PairCandidate[];
};

export type MarketProfileResponse = {
  symbol: string;
  name?: string | null;
  instrument_token: number;
  exchange: string;
  period_minutes: number;
  lookback_days: number;
  value_area_pct: number;
  first_bar_ts: string;
  last_bar_ts: string;
  n_bars: number;
  composite: CompositeProfile | null;
  daily?: DayProfile[];
};
