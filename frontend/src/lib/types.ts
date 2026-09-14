/** Mirrors the FastAPI response shapes — keep in sync with backend/routers/*.py */

/** Dashboard session (the password gate). */
export type SessionStatus = {
  authenticated: boolean;
};

/** Broker session, returned by /api/auth/status. */
export type AuthStatus = {
  authenticated: boolean;
  broker: string;
  display_name: string;
  login_style: "oauth" | "headless" | string;
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
  /** Rolling windows the pair cleared cointegration in (persistent screen only; null for baseline). */
  persistence_count: number | null;
  /** Comma-separated window indices, e.g. "6,7,8" (persistent screen only; null for baseline). */
  persistence_windows: string | null;
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

// ── Buy-on-Gap (intraday mean reversion) ──

export type BuyOnGapDailyRow = {
  date: string;
  has_data: boolean;
  /** This session's P&L delta (session realized + unrealized delta). */
  day_pnl: number | null;
  /** This session's realized delta alone (net of costs). */
  day_realized: number | null;
  n_closed_trades: number | null;
  n_open_positions: number | null;
  win_rate: number | null;
  /** Cumulative book P&L as of this EOD (already cumulative — not re-summed). */
  cumulative_net_pnl: number | null;
};

export type BuyOnGapOpenPosition = {
  symbol: string;
  entry_px: number;
  qty: number;
  stop_px: number;
  gap_z: number;
  last_mtm_px: number;
  pnl: number;
};

export type BuyOnGapSummary = {
  system: string;
  n_days_with_data: number;
  latest_date: string | null;
  realized_pnl: number;
  unrealized_pnl: number;
  net_pnl: number;
  transaction_costs: number;
  n_closed_trades: number;
  n_open_positions: number;
  win_rate: number;
  universe_size: number | null;
};

export type BuyOnGapResponse = {
  start_date: string;
  end_date: string;
  system: string;
  summary: BuyOnGapSummary;
  daily: BuyOnGapDailyRow[];
  open_positions: BuyOnGapOpenPosition[];
};

// Market-Profile trend_up paper book (runners/run_paper_mp.py → dashboard.db).
export type MpTrendDailyRun = {
  date: string;
  n_trend_up: number;
  n_opened: number;
  n_closed: number;
  day_net: number;
  /** Cumulative realized net as of this run (already cumulative). */
  cum_net: number;
  halted: boolean;
  reason: string | null;
};

export type MpTrendOpenPosition = {
  symbol: string;
  entry_date: string;
  entry_px: number;
  qty: number;
  notional: number;
};

export type MpTrendSummary = {
  net_pnl: number;
  gross_pnl: number;
  costs: number;
  n_closed_trades: number;
  n_open_positions: number;
  win_rate: number;
  latest_date: string | null;
  halted: boolean;
  halt_reason: string | null;
};

export type MpTrendResponse = {
  summary: MpTrendSummary;
  daily: MpTrendDailyRun[];
  open_positions: MpTrendOpenPosition[];
};

// ── Kalman pairs (time-varying hedge ratio) ──

export type KalmanPair = {
  pair: string;
  model: string | null;
  /** γ_t the filter currently tracks (live predicted hedge ratio / elasticity). */
  gamma: number | null;
  /** μ_t the filter currently tracks (intercept). */
  mu: number | null;
  position: string; // FLAT | LONG_SPREAD | SHORT_SPREAD
  /** Rolling z-score of the Kalman spread. */
  current_z: number | null;
  /** z the open position was entered at (0 while flat). */
  entry_z: number | null;
  /** ADF regime gate: p-value of the raw-residual window, whether new entries
   *  are permitted, and whether the window is stale (predates a data gap). */
  regime_adf_p: number | null;
  regime_gate_open: boolean | null;
  regime_stale: boolean | null;
  /** Structure risk band (₹), present only while a position is open. */
  stop_inr: number | null;
  target_inr: number | null;
  spread_std: number | null;
  day_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  n_closed_trades: number;
  spread_history_size: number | null;
};

export type KalmanPairsResponse = {
  latest_date: string | null;
  /** Total Kalman EOD sidecars on disk (≤ end) — how many sessions recorded. */
  n_sessions_recorded: number;
  n_pairs: number;
  /** P&L of the LATEST session only (not cumulative across n_sessions_recorded). */
  session_pnl: number;
  pairs: KalmanPair[];
};

// ── Kalman trend (loop-engineering pilot: Kalman-vs-MA A/B + loop memory) ──

/** One closed fill from the latest session (EOD sidecar). pnl is net of costs. */
export type SessionTrade = {
  side: number; // +1 long / -1 short
  entry_price: number;
  exit_price: number;
  pnl_points: number;
  pnl_rupees: number;
  reason: string; // "target" | "stop" | "force_close"
};

export type TrendBook = {
  signal_kind: string; // "kalman" | "ma"
  realized_rupees: number;
  n_trades: number;
  /** Current live position (-1 short / 0 flat / +1 long), from the runner state file. */
  open_pos: number;
  /** THIS session's net ₹ (realized_rupees above is cumulative across the run). */
  session_realized_rupees: number;
  /** THIS session's fills; empty for sessions recorded before this shipped. */
  session_trades: SessionTrade[];
};

export type TrendInstrument = {
  symbol: string;
  kalman: TrendBook;
  ma: TrendBook;
  /** Kalman − MA realized ₹ for this instrument (the A/B's whole point). */
  edge_rupees: number;
};

/** The loop's last-run header, read from STATE.md (values are strings as stored). */
export type LoopStatus = {
  timestamp: string | null;
  status: string | null;
  /** 'pass' | 'REJECT: …' | 'skipped:…' | 'deferred…' */
  checker: string | null;
  /** 'ok' | 'HALT_NEW_ENTRIES' | null (unknown) */
  risk: string | null;
};

export type KalmanTrendResponse = {
  latest_date: string | null;
  n_sessions_recorded: number;
  total_kalman_rupees: number;
  total_ma_rupees: number;
  /** Kalman − MA realized ₹ for the LATEST session (not cumulative). */
  kalman_minus_ma_rupees: number;
  instruments: TrendInstrument[];
  /** Loop-engineering memory (null when STATE.md has no run yet). */
  loop: LoopStatus | null;
  lessons: string[]; // newest-first
};

// ── MA momentum (§6.3 frozen-MA paper holdout) ──

export type MaInstrument = {
  symbol: string;
  /** Front-month future actually being quoted (from the live state file). */
  tradingsymbol: string;
  /** Current live position (-1 short / 0 flat / +1 long). */
  open_pos: number;
  /** Cumulative across the run (NOT this session). */
  realized_rupees: number;
  n_trades: number;
  win_rate: number | null;
  session_realized_rupees: number;
  session_trades: SessionTrade[];
  /** Frozen SMA windows, from the strategy module (not echoed from the sidecar). */
  short: number | null;
  long: number | null;
  /** ₹ by which this leg's stop fills are optimistic (level fill vs 30s poll). */
  stop_overshoot_rupees: number;
  n_stop_fills: number;
  /** True when this row is carried history, not a session result (the runner
   *  could not load the symbol that session). */
  carried: boolean;
};

export type HaltStatus = {
  entries_halted: boolean;
  reasons: string[];
  /** The daily-loss flag persists across sessions — a breach pauses the holdout. */
  daily_loss_flag: boolean;
  /** The heartbeat tripped and the runner EXITED — not merely halted; nothing
   *  is managing an open position either. */
  runner_silent_fail: boolean;
};

export type MaMomentumResponse = {
  latest_date: string | null;
  /** Every sidecar on disk — NOT progress: a halted session still writes one. */
  n_sessions_recorded: number;
  /** Sidecars written with entries LIVE — the only honest progress measure. */
  n_sessions_measured: number;
  /** §6.3 pre-registered holdout length. */
  holdout_sessions: number;
  /** Cumulative realized ₹ as the runner books it — the pre-registered number. */
  total_rupees: number;
  /** Same figure corrected for stop-fill overshoot. Shown alongside, never instead. */
  total_rupees_ex_overshoot: number;
  stop_overshoot_rupees: number;
  n_stop_fills: number;
  n_trades: number;
  instruments: MaInstrument[];
  halt: HaltStatus;
  /** When the runner last wrote its state file (ISO), or null. */
  state_updated: string | null;
  /** True when that write predates the latest session — positions are NOT live. */
  positions_stale: boolean;
  /** Symbols whose row is carried history rather than a session result. */
  carried_symbols: string[];
  note: string;
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

// ── Cross-strategy portfolio exposure (/api/portfolio/exposure) ──────────────
export type UnderlyingExposure = {
  underlying: string;
  net_delta1_units: number;         // futures + equity + futures-hedge (exact)
  net_option_delta: number | null;  // live greeks; null offline or if unpriced
  net_total_delta: number | null;   // delta1 + option delta; null when unknown
  net_notional: number;
  systems: string[];
  shared: boolean;                  // held by >1 strategy
  has_options: boolean;
};

export type BrokerPosition = {
  tradingsymbol: string;
  exchange: string;
  quantity: number;
  average_price: number;
  pnl: number;
};

export type PortfolioResponse = {
  live: boolean;                    // a working Kite session augmented this view
  note: string;
  underlyings: UnderlyingExposure[];
  broker_net: BrokerPosition[] | null;
};

// ── Short-call-into-earnings (paper) ────────────────────────────────────────
// NOTE: the strategy this describes has NO measured edge. See
// docs/research/pre-earnings-iv-crush-2026-08-29.md. `health_note` carries that
// through to the UI on purpose.

export type ShortCallUpcomingEvent = {
  symbol: string;
  event_date: string;
  sessions_until: number | null;
  announced_at: string | null;
  ivp: number | null;
  ivp_basis: string;
  atm_iv: number | null;
  spot: number | null;
  strike: number | null;
  dte: number | null;
  est_credit: number | null;
  lot_size: number | null;
  est_lots: number | null;
  target_px: number | null;
  stop_px: number | null;
  r_rupees: number | null;
  est_gross_credit: number | null;
  qualifies: boolean;
  blocked_by: string | null;
};

export type ShortCallIvpRow = {
  symbol: string;
  ivp: number;
  atm_iv: number;
  spot: number;
  dte: number;
  next_results: string | null;
  days_to_results: number | null;
};

export type ShortCallUpcomingResponse = {
  as_of: string;
  panel_through: string | null;
  entry_ivp_min: number;
  n_results_meetings: number;
  n_in_fno_universe: number;
  events: ShortCallUpcomingEvent[];
  top_ivp: ShortCallIvpRow[];
  note: string;
};

export type ShortCallOpenPosition = {
  symbol: string;
  tradingsymbol: string;
  event_date: string;
  strike: number;
  expiry: string;
  entry_dt: string | null;
  credit: number;
  lots: number;
  lot_size: number;
  target_px: number;
  stop_px: number;
  r_rupees: number;
  ivp_at_entry: number;
  last_mtm_px: number;
  sessions_held: number;
  unrealized: number;
  unrealized_R: number | null;
};

export type ShortCallClosedTrade = {
  symbol: string;
  event_date: string;
  entry_dt: string | null;
  exit_dt: string | null;
  credit: number;
  exit_px: number | null;
  lots: number;
  lot_size: number;
  exit_reason: string | null;
  pnl: number;
  realised_R: number;
  ivp_at_entry: number;
};

export type ShortCallDailyRow = {
  date: string;
  has_data: boolean;
  day_pnl: number | null;
  day_closed: number | null;
  open_positions: number | null;
  gap_through_stop: number | null;
};

export type ShortCallSummary = {
  latest_date: string | null;
  n_days_with_data: number;
  realized_pnl: number;
  unrealized_pnl: number;
  transaction_costs: number;
  n_closed_trades: number;
  n_open_positions: number;
  win_rate: number | null;
  mean_realised_R: number | null;
  worst_realised_R: number | null;
  gap_through_stop_count: number;
  gap_through_worst_R: number | null;
  exit_reasons: Record<string, number>;
  health_note: string;
};

export type ShortCallResponse = {
  start_date: string;
  end_date: string;
  params: Record<string, number>;
  summary: ShortCallSummary;
  daily: ShortCallDailyRow[];
  open_positions: ShortCallOpenPosition[];
  closed_trades: ShortCallClosedTrade[];
};
