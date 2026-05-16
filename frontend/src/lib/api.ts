import type {
  AuthStatus,
  EquityPositionsResponse,
  EquityScansResponse,
  EquitySignalsResponse,
  FiiDiiResponse,
  MarketProfileResponse,
  MarketProfileSymbol,
  PairCandidatesResponse,
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
 */
async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
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
