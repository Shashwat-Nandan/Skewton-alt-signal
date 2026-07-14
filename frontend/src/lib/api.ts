import type {
  ArbitrageResponse,
  AuthStatus,
  BuyOnGapResponse,
  EquityPendingEntriesResponse,
  EquityPositionsResponse,
  EquityScansResponse,
  EquitySignalsResponse,
  FiiDiiResponse,
  KalmanPairsResponse,
  KalmanTrendResponse,
  MarketProfileResponse,
  MarketProfileSymbol,
  MpTrendResponse,
  PairCandidatesResponse,
  PaperCompareResponse,
  PositionsResponse,
  RunDetail,
  RunSummary,
  SessionStatus,
  StrategyInfo,
} from "./types";

/**
 * Thrown when an API call returns 401. The dashboard session expired or
 * was never established; the caller should kick the user back to the
 * login page. App.tsx's QueryClient onError handler reacts to this.
 */
export class UnauthorizedError extends Error {
  constructor() {
    super("Unauthorized");
    this.name = "UnauthorizedError";
  }
}

/**
 * Thin typed wrapper around fetch. The backend is same-origin in dev (Vite
 * proxy) and same-origin in prod (served behind the same reverse proxy), so
 * we never set an Origin or include credentials explicitly.
 *
 * Every API call goes through /api/* so the path space stays disjoint from
 * the SPA's client-side router. Without this, a hard refresh on a route like
 * /positions hit the nginx API regex and returned JSON instead of the SPA.
 */
const API_BASE = "/api";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-json body */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  // Dashboard session (the password gate in front of everything below)
  sessionStatus: () => http<SessionStatus>("/session/me"),
  sessionLogin: (password: string) =>
    http<void>("/session/login", { method: "POST", body: JSON.stringify({ password }) }),
  sessionLogout: () => http<void>("/session/logout", { method: "POST" }),

  // Kite OAuth (the broker session, gated behind the dashboard session)
  authStatus: () => http<AuthStatus>("/auth/status"),
  loginUrl: () => http<{ login_url: string }>("/auth/login"),
  logout: () => http<void>("/auth/logout", { method: "POST" }),

  listStrategies: () => http<StrategyInfo[]>("/strategies"),

  listRuns: () => http<RunSummary[]>("/runs"),
  getRun: (id: string) => http<RunDetail>(`/runs/${id}`),
  createRun: (body: { strategy: string; mode: string; params: Record<string, unknown> }) =>
    http<RunSummary>("/runs", { method: "POST", body: JSON.stringify(body) }),
  stopRun: (id: string) => http<RunSummary>(`/runs/${id}/stop`, { method: "POST" }),

  pairCandidates: (top?: number) => {
    const qs = top != null ? `?top=${top}` : "";
    return http<PairCandidatesResponse>(`/pair-candidates${qs}`);
  },

  pairCandidatesPersistent: (top?: number) => {
    const qs = top != null ? `?top=${top}` : "";
    return http<PairCandidatesResponse>(`/pair-candidates/persistent${qs}`);
  },

  pairPaperCompare: (params: { days?: number; end?: string; systems?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.days != null) q.set("days", String(params.days));
    if (params.end) q.set("end", params.end);
    if (params.systems) q.set("systems", params.systems);
    const qs = q.toString();
    return http<PaperCompareResponse>(`/pair-paper-compare${qs ? `?${qs}` : ""}`);
  },

  arbitragePaper: (params: { days?: number; end?: string; system?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.days != null) q.set("days", String(params.days));
    if (params.end) q.set("end", params.end);
    if (params.system) q.set("system", params.system);
    const qs = q.toString();
    return http<ArbitrageResponse>(`/arbitrage-paper${qs ? `?${qs}` : ""}`);
  },

  buyOnGapPaper: (params: { days?: number; end?: string; system?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.days != null) q.set("days", String(params.days));
    if (params.end) q.set("end", params.end);
    if (params.system) q.set("system", params.system);
    const qs = q.toString();
    return http<BuyOnGapResponse>(`/buy-on-gap-paper${qs ? `?${qs}` : ""}`);
  },

  mpTrend: (params: { days?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.days != null) q.set("days", String(params.days));
    const qs = q.toString();
    return http<MpTrendResponse>(`/mp-trend${qs ? `?${qs}` : ""}`);
  },

  kalmanPairs: (params: { end?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.end) q.set("end", params.end);
    const qs = q.toString();
    return http<KalmanPairsResponse>(`/kalman-pairs${qs ? `?${qs}` : ""}`);
  },

  kalmanTrend: (params: { end?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.end) q.set("end", params.end);
    const qs = q.toString();
    return http<KalmanTrendResponse>(`/kalman-trend${qs ? `?${qs}` : ""}`);
  },

  equityPositions: (status?: "open" | "closed") => {
    const qs = status ? `?status=${status}` : "";
    return http<EquityPositionsResponse>(`/equity/positions${qs}`);
  },
  equitySignals: (date?: string) => {
    const qs = date ? `?date=${encodeURIComponent(date)}` : "";
    return http<EquitySignalsResponse>(`/equity/signals${qs}`);
  },
  equityScans: (limit?: number) => {
    const qs = limit != null ? `?limit=${limit}` : "";
    return http<EquityScansResponse>(`/equity/scans${qs}`);
  },
  equityFiiDii: (days?: number) => {
    const qs = days != null ? `?days=${days}` : "";
    return http<FiiDiiResponse>(`/equity/fii-dii${qs}`);
  },
  // EQ-FU-1
  equityPendingEntries: (status?: string) => {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return http<EquityPendingEntriesResponse>(`/equity/pending-entries${qs}`);
  },

  positions: () => http<PositionsResponse>("/positions"),

  marketProfileSymbols: () =>
    http<MarketProfileSymbol[]>("/market-profile/symbols"),
  marketProfile: (
    symbol: string,
    params: { days?: number; period_minutes?: number; mode?: "composite" | "daily" } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.days != null) q.set("days", String(params.days));
    if (params.period_minutes != null) q.set("period_minutes", String(params.period_minutes));
    if (params.mode) q.set("mode", params.mode);
    const qs = q.toString();
    return http<MarketProfileResponse>(
      `/market-profile/${encodeURIComponent(symbol)}${qs ? `?${qs}` : ""}`,
    );
  },
};
