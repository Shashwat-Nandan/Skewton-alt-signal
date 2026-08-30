import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Info } from "lucide-react";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatINR, formatNum } from "@/lib/utils";

const POLL_MS = 60_000;
const WINDOW_DAYS = 20;
const UPCOMING_DAYS = 30;

function pnlClass(v: number): string {
  return cn("tabular-nums", v > 0 && "text-emerald-600", v < 0 && "text-rose-600");
}

function MetricCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
        {hint ? (
          <div className="mt-1 text-xs text-muted-foreground">{hint}</div>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** IVP colouring: the entry gate is 90, so make ≥90 unmistakable. */
function IvpBadge({ ivp, gate }: { ivp: number | null; gate: number }) {
  if (ivp == null) return <span className="text-muted-foreground">—</span>;
  const hot = ivp >= gate;
  const warm = ivp >= gate - 15 && !hot;
  return (
    <Badge
      variant={hot ? "default" : "secondary"}
      className={cn(
        "tabular-nums",
        hot && "bg-rose-600 hover:bg-rose-600",
        warm && "bg-amber-500/15 text-amber-700 hover:bg-amber-500/15",
      )}
    >
      {ivp.toFixed(0)}
    </Badge>
  );
}

function ExitBadge({ reason }: { reason: string | null }) {
  if (!reason) return <span className="text-muted-foreground">—</span>;
  const bad = reason === "GAP_STOP";
  const stop = reason === "STOP";
  return (
    <Badge
      variant="secondary"
      className={cn(
        "font-mono text-[11px]",
        bad && "bg-rose-600 text-white hover:bg-rose-600",
        stop && "bg-amber-500/15 text-amber-700 hover:bg-amber-500/15",
        reason === "TARGET" && "bg-emerald-500/15 text-emerald-700 hover:bg-emerald-500/15",
      )}
    >
      {reason}
    </Badge>
  );
}

export function ShortCallPage() {
  const book = useQuery({
    queryKey: ["short-call", WINDOW_DAYS],
    queryFn: () => api.shortCall({ days: WINDOW_DAYS }),
    refetchInterval: POLL_MS,
  });
  const upcoming = useQuery({
    queryKey: ["short-call-upcoming", UPCOMING_DAYS],
    queryFn: () => api.shortCallUpcoming({ days: UPCOMING_DAYS }),
    refetchInterval: POLL_MS,
  });

  if (book.isLoading || upcoming.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (book.error || upcoming.error) {
    return (
      <Card>
        <CardContent className="pt-6 text-sm text-rose-600">
          Failed to load the short-call book. If this is a fresh deploy, the
          dashboard backend may be stale — it has no auto-deploy, so a new
          router 404s until <code>systemctl restart dashboard-backend.service</code>.
        </CardContent>
      </Card>
    );
  }

  const s = book.data!.summary;
  const up = upcoming.data!;
  const gate = up.entry_ivp_min;

  return (
    <div className="space-y-6">
      {/* The verdict, first thing on the page. A populated table must never be
          mistaken for a validated signal. */}
      <Card className="border-amber-500/40 bg-amber-500/5">
        <CardContent className="flex gap-3 pt-5 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <div>
            <span className="font-semibold">No measured edge — forward test only.</span>{" "}
            The earnings event is fairly priced (implied E|jump| 3.43% vs realised
            3.38%; breach 41.3% against a 42.4% fair-value benchmark). The backtest
            for this structure is 100% directional and statistically
            indistinguishable from zero: 356 trades, +₹92,091,{" "}
            <span className="font-mono">t=0.33</span> — a zero-edge process beats
            that 37% of the time, and all of it is 2025. Paper only; live mode
            raises. See{" "}
            <code className="text-xs">
              docs/research/pre-earnings-iv-crush-2026-08-29.md
            </code>
            .
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <MetricCard label="Realized P&L" value={formatINR(s.realized_pnl, 0)} />
        <MetricCard
          label="Open / Closed"
          value={`${s.n_open_positions} / ${s.n_closed_trades}`}
        />
        <MetricCard
          label="Win rate"
          value={s.win_rate == null ? "—" : `${s.win_rate.toFixed(0)}%`}
        />
        <MetricCard
          label="Mean realised R"
          value={s.mean_realised_R == null ? "—" : formatNum(s.mean_realised_R, 3)}
          hint={
            s.worst_realised_R == null
              ? undefined
              : `worst ${formatNum(s.worst_realised_R, 2)}R`
          }
        />
        {/* The number this whole run exists to produce. */}
        <MetricCard
          label="Gapped past stop"
          value={String(s.gap_through_stop_count)}
          hint={
            s.gap_through_worst_R == null
              ? "the metric this run exists to measure"
              : `worst ${formatNum(s.gap_through_worst_R, 2)}R vs a nominal −1R`
          }
        />
      </div>

      {/* ── Upcoming results, with the trade that would be placed ── */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">
            Upcoming results — next {UPCOMING_DAYS} days
          </CardTitle>
          <div className="text-xs text-muted-foreground">
            {up.n_results_meetings} results meetings in window,{" "}
            <span className="font-medium">{up.n_in_fno_universe}</span> in the F&amp;O
            universe. Entry gate IVP ≥ {gate.toFixed(0)}. Panel through{" "}
            {up.panel_through ?? "—"}.
          </div>
        </CardHeader>
        <CardContent>
          {up.events.length === 0 ? (
            <div className="flex gap-2 rounded-md bg-muted/40 p-3 text-sm text-muted-foreground">
              <Info className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                No F&amp;O-universe results scheduled in this window. Between
                earnings seasons this is expected, not a fault — Q2 intimations
                are filed roughly two weeks ahead. The runner will no-op daily
                until then.
              </span>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Stock</TableHead>
                  <TableHead>Results</TableHead>
                  <TableHead className="text-right">IVP</TableHead>
                  <TableHead className="text-right">ATM IV</TableHead>
                  <TableHead className="text-right">Spot</TableHead>
                  <TableHead className="text-right">Strike</TableHead>
                  <TableHead className="text-right">Credit</TableHead>
                  <TableHead className="text-right">Target</TableHead>
                  <TableHead className="text-right">Stop</TableHead>
                  <TableHead className="text-right">Lots</TableHead>
                  <TableHead className="text-right">1R</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {up.events.map((e) => (
                  <TableRow key={`${e.symbol}-${e.event_date}`}>
                    <TableCell className="font-medium">{e.symbol}</TableCell>
                    <TableCell className="tabular-nums">
                      {e.event_date}
                      {e.sessions_until != null ? (
                        <span className="ml-1 text-xs text-muted-foreground">
                          (+{e.sessions_until}d)
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-right">
                      <IvpBadge ivp={e.ivp} gate={gate} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {e.atm_iv == null ? "—" : `${(e.atm_iv * 100).toFixed(1)}%`}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(e.spot, 1)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(e.strike, 0)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(e.est_credit, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-emerald-700">
                      {formatNum(e.target_px, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-rose-700">
                      {formatNum(e.stop_px, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {e.est_lots ?? "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {e.r_rupees == null ? "—" : formatINR(e.r_rupees, 0)}
                    </TableCell>
                    <TableCell className="text-xs">
                      {e.qualifies ? (
                        <Badge className="bg-emerald-600 hover:bg-emerald-600">
                          would enter
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">{e.blocked_by}</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <div className="mt-3 flex gap-2 text-xs text-muted-foreground">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{up.note}</span>
          </div>
        </CardContent>
      </Card>

      {/* ── IVP leaderboard, so the page is useful between seasons ── */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">
            Highest IV percentile now — F&amp;O universe
          </CardTitle>
          <div className="text-xs text-muted-foreground">
            Where implied vol sits against each stock's own trailing year,
            whether or not results are scheduled. A high IVP without a results
            date is not a signal — the research found IVP marks the event, not a
            mispricing.
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Stock</TableHead>
                <TableHead className="text-right">IVP</TableHead>
                <TableHead className="text-right">ATM IV</TableHead>
                <TableHead className="text-right">Spot</TableHead>
                <TableHead className="text-right">DTE</TableHead>
                <TableHead>Next results</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {up.top_ivp.map((r) => (
                <TableRow key={r.symbol}>
                  <TableCell className="font-medium">{r.symbol}</TableCell>
                  <TableCell className="text-right">
                    <IvpBadge ivp={r.ivp} gate={gate} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {(r.atm_iv * 100).toFixed(1)}%
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatNum(r.spot, 1)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{r.dte}</TableCell>
                  <TableCell className="tabular-nums">
                    {r.next_results ?? (
                      <span className="text-muted-foreground">not scheduled</span>
                    )}
                    {r.days_to_results != null ? (
                      <span className="ml-1 text-xs text-muted-foreground">
                        (+{r.days_to_results}d)
                      </span>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* ── Open positions ── */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Open positions</CardTitle>
        </CardHeader>
        <CardContent>
          {book.data!.open_positions.length === 0 ? (
            <div className="text-sm text-muted-foreground">No open positions.</div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Contract</TableHead>
                  <TableHead>Results</TableHead>
                  <TableHead className="text-right">IVP @ entry</TableHead>
                  <TableHead className="text-right">Credit</TableHead>
                  <TableHead className="text-right">Mark</TableHead>
                  <TableHead className="text-right">Target</TableHead>
                  <TableHead className="text-right">Stop</TableHead>
                  <TableHead className="text-right">Lots</TableHead>
                  <TableHead className="text-right">1R</TableHead>
                  <TableHead className="text-right">Unrealised</TableHead>
                  <TableHead className="text-right">R</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {book.data!.open_positions.map((p) => (
                  <TableRow key={p.tradingsymbol}>
                    <TableCell className="font-mono text-xs">
                      {p.tradingsymbol}
                    </TableCell>
                    <TableCell className="tabular-nums">{p.event_date}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {p.ivp_at_entry.toFixed(0)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(p.credit, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(p.last_mtm_px, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-emerald-700">
                      {formatNum(p.target_px, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-rose-700">
                      {formatNum(p.stop_px, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{p.lots}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatINR(p.r_rupees, 0)}
                    </TableCell>
                    <TableCell className={cn("text-right", pnlClass(p.unrealized))}>
                      {formatINR(p.unrealized, 0)}
                    </TableCell>
                    <TableCell
                      className={cn(
                        "text-right",
                        pnlClass(p.unrealized_R ?? 0),
                      )}
                    >
                      {p.unrealized_R == null ? "—" : formatNum(p.unrealized_R, 2)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* ── Closed trades ── */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Closed trades</CardTitle>
          <div className="text-xs text-muted-foreground">
            Realised R is the honest metric: a stop that gaps through fills worse
            than the nominal −1R, and{" "}
            <span className="font-mono">GAP_STOP</span> marks exactly those.
          </div>
        </CardHeader>
        <CardContent>
          {book.data!.closed_trades.length === 0 ? (
            <div className="text-sm text-muted-foreground">No closed trades yet.</div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Stock</TableHead>
                  <TableHead>Results</TableHead>
                  <TableHead>Exit</TableHead>
                  <TableHead className="text-right">IVP @ entry</TableHead>
                  <TableHead className="text-right">Credit</TableHead>
                  <TableHead className="text-right">Exit px</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead className="text-right">P&amp;L</TableHead>
                  <TableHead className="text-right">Realised R</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {book.data!.closed_trades.map((t, i) => (
                  <TableRow key={`${t.symbol}-${t.event_date}-${i}`}>
                    <TableCell className="font-medium">{t.symbol}</TableCell>
                    <TableCell className="tabular-nums">{t.event_date}</TableCell>
                    <TableCell className="tabular-nums text-xs">
                      {t.exit_dt?.slice(0, 10) ?? "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {t.ivp_at_entry.toFixed(0)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(t.credit, 2)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(t.exit_px, 2)}
                    </TableCell>
                    <TableCell>
                      <ExitBadge reason={t.exit_reason} />
                    </TableCell>
                    <TableCell className={cn("text-right", pnlClass(t.pnl))}>
                      {formatINR(t.pnl, 0)}
                    </TableCell>
                    <TableCell className={cn("text-right", pnlClass(t.realised_R))}>
                      {formatNum(t.realised_R, 2)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
