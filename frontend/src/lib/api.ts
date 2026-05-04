import type {
  AuthStatus,
  MarketProfileResponse,
  MarketProfileSymbol,
  PairCandidatesResponse,
  RunDetail,
  RunSummary,
  StrategyInfo,
} from "./types";

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
  authStatus: () => http<AuthStatus>("/auth/status"),
  loginUrl: () => http<{ login_url: string }>("/auth/login"),
  logout: () => http<void>("/auth/logout", { method: "POST" }),

  listStrategies: () => http<StrategyInfo[]>("/strategies"),

  listRuns: () => http<RunSummary[]>("/runs"),
  getRun: (id: string) => http<RunDetail>(`/runs/${id}`),
  createRun: (body: { strategy: string; mode: string; params: Record<string, unknown> }) =>
    http<RunSummary>("/runs", { method: "POST", body: JSON.stringify(body) }),
  stopRun: (id: string) => http<RunSummary>(`/runs/${id}/stop`, { method: "POST" }),

  pairCandidates: () => http<PairCandidatesResponse>("/pair-candidates"),

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
