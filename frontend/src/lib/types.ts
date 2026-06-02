/** Mirrors the FastAPI response shapes — keep in sync with backend/routers/*.py */

/** Dashboard session (the password gate). */
export type SessionStatus = {
  authenticated: boolean;
};

/** Kite/broker session, returned by /api/auth/status. */
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

export type PairSkipReason = "beta" | "quality" | "leg_cap" | "cutoff";

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
  /** 1..N admit order under the requested `top` cutoff; null if the runner skips. */
  processing_rank: number | null;
  /** Why the runner skipped this candidate ("beta"|"quality"|"leg_cap"|"cutoff"). */
  skip_reason: PairSkipReason | null;
};

export type PairCandidatesResponse = {
  generated_at: string | null;
  top: number;
  candidates: PairCandidate[];
};

// ── Paper-system comparison (baseline vs persistent) ──

export type PaperSystemDay = {
  net_pnl: number;
  n_pairs: number;
  n_trades: number;
  costs: number;
} | null;

export type PaperCompareDailyRow = {
  date: string;
  systems: Record<string, PaperSystemDay>;
};

export type PaperCompareAggregate = {
  system: string;
  net_pnl: number;
  n_days_with_data: number;
  avg_per_day: number;
  n_unique_pairs: number;
  n_closed_trades: number;
  transaction_costs: number;
};

export type PaperComparePerPair = {
  pair: string;
  /** Map of system name → cumulative net P&L over the window, or null if not traded. */
  by_system: Record<string, number | null>;
  /** 'BOTH' or 'only <sys>,<sys>'. */
  traded_by: string;
};

export type PaperCompareResponse = {
  start_date: string;
  end_date: string;
  systems: string[];
  daily: PaperCompareDailyRow[];
  aggregate: PaperCompareAggregate[];
  per_pair: PaperComparePerPair[];
};

// ── Arbitrage (calendar/term-structure spreads) ──

export type ArbitrageDailyRow = {
  date: string;
  has_data: boolean;
  /** This session's P&L delta (session realized + unrealized delta). */
  day_pnl: number | null;
  /** This session's realized delta alone. */
  day_realized: number | null;
  n_closed_trades: number | null;
  n_open_calendars: number | null;
  /** Cumulative book P&L as of this EOD (already cumulative — not re-summed). */
  cumulative_net_pnl: number | null;
};

export type ArbitrageOpenCalendar = {
  symbol: string;
  position: string;
  entry_carry_diff: number;
  legs: Record<string, unknown>[];
};

export type ArbitrageSummary = {
  system: string;
  n_days_with_data: number;
  latest_date: string | null;
  realized_pnl: number;
  unrealized_pnl: number;
  net_pnl: number;
  transaction_costs: number;
  n_closed_trades: number;
  n_open_calendars: number;
  universe_size: number | null;
};

export type ArbitrageResponse = {
  start_date: string;
  end_date: string;
  system: string;
  summary: ArbitrageSummary;
  daily: ArbitrageDailyRow[];
  open_calendars: ArbitrageOpenCalendar[];
};

// ── Equity Swing ──

export type EquityPosition = {
  id: number;
  symbol: string;
  side: string;
  entry_dt: string;
  entry_px: number;
  qty: number;
  initial_sl: number;
  current_sl: number;
  target: number;
  atr_at_entry: number;
  rationale?: string | null;
  last_mtm_dt?: string | null;
  last_mtm_px?: number | null;
  high_watermark?: number | null;
  status: "OPEN" | "CLOSED";
  exit_dt?: string | null;
  exit_px?: number | null;
  exit_reason?: string | null;
  pnl?: number | null;
  opened_by_scan?: string | null;
};

export type EquityPositionsResponse = {
  positions: EquityPosition[];
};

export type EquitySignal = {
  timestamp: string;
  tradingsymbol: string;
  transaction_type: "BUY" | "SELL";
  quantity: number;
  price: number;
  rationale?: string | null;
};

export type EquitySignalsResponse = {
  date: string;
  generated_at: string | null;
  signals: EquitySignal[];
};

export type EquityScan = {
  id: number;
  scan_dt: string;
  scan_kind: "open" | "close";
  mode: "signals" | "paper";
  n_signals: number;
  n_trades: number;
  n_open_positions: number;
  n_closed_today: number;
  notes?: string | null;
};

export type EquityScansResponse = {
  scans: EquityScan[];
};

export type FiiDiiRow = {
  date: string;
  fii_net: number | null;
  dii_net: number | null;
  fii_net_5d: number | null;
  dii_net_5d: number | null;
  fii_boost: number | null;
};

export type FiiDiiResponse = {
  generated_at: string | null;
  rows: FiiDiiRow[];
};

// EQ-FU-1: equity_pending_entries surface
export type EquityPendingStatus =
  | "PENDING"
  | "FILLED"
  | "SKIPPED_GAP"
  | "SKIPPED_STALE"
  | "SKIPPED_OPEN";

export type EquityPendingEntry = {
  id: number;
  signal_dt: string;
  symbol: string;
  side: string;
  signal_close: number;
  sl_distance: number;
  target_distance: number;
  atr: number;
  qty: number;
  rationale: string | null;
  status: EquityPendingStatus;
  created_at: string;
  resolved_at: string | null;
  resolution_note: string | null;
};

export type EquityPendingEntriesResponse = {
  pending: EquityPendingEntry[];
};

// ── Live position tracker ──

export type PositionMode = "paper" | "live";

export type OpenPosition = {
  group: string;
  tradingsymbol: string;
  side: "LONG" | "SHORT";
  quantity: number;
  lot_size: number;
  entry_price: number;
  current_price: number | null;
  unrealized_pnl: number | null;
  entry_time: string | null;
  note: string | null;
};

export type ClosedTrade = {
  group: string;
  entry_time: string | null;
  exit_time: string | null;
  realized_pnl: number;
  transaction_costs: number | null;
  note: string | null;
};

export type PositionSystemSummary = {
  realized_pnl: number;
  unrealized_pnl: number;
  transaction_costs: number;
  total_pnl: number;
  n_open_positions: number;
  n_closed_today: number;
};

export type PositionSystemBlock = {
  name: string;
  label: string;
  mode: PositionMode;
  state_file: string;
  updated_at: string | null;
  available: boolean;
  summary: PositionSystemSummary;
  open_positions: OpenPosition[];
  closed_today: ClosedTrade[];
};

export type PositionsResponse = {
  generated_at: string;
  systems: PositionSystemBlock[];
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
