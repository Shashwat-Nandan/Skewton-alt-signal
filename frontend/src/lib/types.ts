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
