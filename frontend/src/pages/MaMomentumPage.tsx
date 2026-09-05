import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Info, PauseCircle, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
import type { MaInstrument, SessionTrade } from "@/lib/types";

const POLL_MS = 60_000;

function pnlClass(v: number | null | undefined): string {
  return cn(
    "tabular-nums",
    (v ?? 0) > 0 && "text-emerald-600",
    (v ?? 0) < 0 && "text-rose-600",
  );
}

function PosBadge({ pos }: { pos: number }) {
  if (pos > 0)
    return <Badge className="bg-emerald-600/15 text-emerald-700 hover:bg-emerald-600/15">long</Badge>;
  if (pos < 0)
    return <Badge className="bg-rose-600/15 text-rose-700 hover:bg-rose-600/15">short</Badge>;
  return <Badge variant="outline" className="text-muted-foreground">flat</Badge>;
}

function MetricCard({
  label,
  value,
  valueClass,
  hint,
}: {
  label: string;
  value: string;
  valueClass?: string;
  hint?: string;
}) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className={cn("mt-1 text-xl font-semibold tabular-nums", valueClass)}>{value}</div>
        {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  );
}

/** One instrument's fills for the latest session, with the session net in the header. */
function SessionBook({ inst }: { inst: MaInstrument }) {
  const trades = inst.session_trades ?? [];
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium">
          {inst.symbol}{" "}
          <span className="text-xs font-normal text-muted-foreground">
            ({trades.length} {trades.length === 1 ? "trade" : "trades"})
          </span>
        </span>
        <span className="text-xs text-muted-foreground">
          net{" "}
          <span className={cn("font-medium", pnlClass(inst.session_realized_rupees))}>
            {formatINR(inst.session_realized_rupees)}
          </span>
        </span>
      </div>
      {trades.length === 0 ? (
        <p className="text-xs text-muted-foreground">No trades this session.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Side</TableHead>
              <TableHead className="text-right">Entry</TableHead>
              <TableHead className="text-right">Exit</TableHead>
              <TableHead className="text-right">P&amp;L</TableHead>
              <TableHead>Reason</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {trades.map((t: SessionTrade, idx: number) => (
              <TableRow key={idx}>
                <TableCell><PosBadge pos={t.side} /></TableCell>
                <TableCell className="text-right tabular-nums text-muted-foreground">{formatNum(t.entry_price)}</TableCell>
                <TableCell className="text-right tabular-nums text-muted-foreground">{formatNum(t.exit_price)}</TableCell>
                <TableCell className={cn("text-right", pnlClass(t.pnl_rupees))}>{formatINR(t.pnl_rupees)}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{t.reason}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

export function MaMomentumPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["ma-momentum"],
    queryFn: () => api.maMomentum(),
    refetchInterval: POLL_MS,
  });

  if (isLoading) return <Skeleton className="h-96" />;
  if (isError)
    return (
      <Card>
        <CardContent className="pt-6 text-sm text-rose-600">
          Failed to load MA-momentum holdout data.
        </CardContent>
      </Card>
    );

  const instruments = data?.instruments ?? [];
  const hasData = data?.latest_date != null;
  const halt = data?.halt;
  const halted = !!halt?.entries_halted;
  const silentFail = !!halt?.runner_silent_fail;
  // A dead runner is not a healthy runner: SILENT_FAIL means the process
  // exited, so entries are stopped AND nothing manages an open position.
  const unhealthy = halted || silentFail;
  const measured = data?.n_sessions_measured ?? 0;
  const recorded = data?.n_sessions_recorded ?? 0;
  const target = data?.holdout_sessions ?? 60;
  const overshoot = data?.stop_overshoot_rupees ?? 0;
  const stale = !!data?.positions_stale;
  const carried = data?.carried_symbols ?? [];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">MA momentum — §6.3 paper holdout</h1>
        <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          <Info className="h-3.5 w-3.5" />
          Frozen-window MA crossover on NIFTY/BANKNIFTY futures, paper-only (live raises).
          Its own backtest is <strong className="mx-1">NO-GO</strong> — OOS-prior Sharpe −0.07 /
          −0.34, combined −₹13,984. This runs to be measured, not because it is believed.
          {data?.latest_date ? ` Latest session ${data.latest_date}.` : ""}
        </p>
      </div>

      {/* Entry-halt status. Rendered ALWAYS, not only when halted: this book's
          daily-loss flag persists across sessions and nothing clears it, so a
          paused holdout keeps writing EOD sidecars and looks "flat" in any
          P&L-only view. Absence of the banner must mean "checked and clear". */}
      <Card className={cn(unhealthy && "border-rose-300")}>
        <CardHeader className="pb-2">
          <CardTitle className="text-base flex items-center gap-2">
            {unhealthy ? (
              <PauseCircle className="h-4 w-4 text-rose-600" />
            ) : (
              <ShieldCheck className="h-4 w-4 text-emerald-600" />
            )}
            {silentFail ? "Runner stopped" : "Entries"}
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          {unhealthy ? (
            <ul className="space-y-1.5">
              {(halt?.reasons ?? []).map((r) => (
                <li key={r} className="flex gap-2 text-rose-700">
                  <span>•</span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-muted-foreground">
              No halt flag present — entries are live. Exits and the 15:25 flatten always run.
            </span>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Realized ₹ (as booked)"
          value={formatINR(data?.total_rupees ?? 0)}
          valueClass={pnlClass(data?.total_rupees)}
          hint="the pre-registered number"
        />
        <MetricCard
          label="Realized ₹ (ex-overshoot)"
          value={formatINR(data?.total_rupees_ex_overshoot ?? 0)}
          valueClass={pnlClass(data?.total_rupees_ex_overshoot)}
          hint="read this one"
        />
        <MetricCard
          label="Stop overshoot"
          value={formatINR(overshoot)}
          hint={`${data?.n_stop_fills ?? 0} stop fills`}
        />
        <MetricCard
          label="Holdout progress"
          value={`${measured} / ${target}`}
          hint={
            recorded > measured
              ? `${recorded - measured} recorded session${recorded - measured === 1 ? "" : "s"} had entries halted and do not count`
              : `${Math.max(target - measured, 0)} sessions left`
          }
        />
      </div>

      {/* The overshoot correction, explained where it is read. Without this the
          raw P&L looks like the result; it is not. */}
      {overshoot !== 0 && (
        <Card className="border-amber-300">
          <CardContent className="pt-5 flex gap-2 text-sm">
            <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
            <span className="text-muted-foreground">
              Stops are booked at their <strong>level</strong>, but the runner polls every 30s —
              BANKNIFTY&apos;s frozen stop is 14.38 pts, smaller than a routine 30s excursion. So{" "}
              {formatINR(overshoot)} of the booked P&amp;L is slippage the fill model handed back
              for free. The <em>ex-overshoot</em> figure is the honest one; the raw figure is kept
              beside it because silently restating a pre-registered number is how a holdout stops
              being a holdout.
            </span>
          </CardContent>
        </Card>
      )}

      {stale && (
        <Card className="border-amber-300">
          <CardContent className="pt-5 flex gap-2 text-sm">
            <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
            <span className="text-muted-foreground">
              The runner has not written its state since{" "}
              <strong>{data?.state_updated?.slice(0, 16)?.replace("T", " ") ?? "an unknown time"}</strong>.
              Any position shown below is the <em>last recorded</em> one, not a live position —
              the process may have exited with it still open. Check the runner before acting on it.
            </span>
          </CardContent>
        </Card>
      )}

      {carried.length > 0 && (
        <Card className="border-amber-300">
          <CardContent className="pt-5 flex gap-2 text-sm">
            <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
            <span className="text-muted-foreground">
              {carried.join(", ")} did not trade in the latest session — the runner could not load
              {carried.length === 1 ? " it" : " them"}, so {carried.length === 1 ? "its" : "their"}{" "}
              row is carried history. Totals are whole, but this session is <strong>partial</strong>.
            </span>
          </CardContent>
        </Card>
      )}

      {!hasData ? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No completed session yet. Positions and per-session P&amp;L appear after the
            <code className="mx-1 rounded bg-muted px-1">ma-momentum-paper</code>
            timer runs its first session (fires 09:18 IST Mon–Fri; sidecar written at 15:25 IST).
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                Instruments{" "}
                <span className="text-xs font-normal text-muted-foreground">
                  ({stale
                    ? `positions last written ${data?.state_updated?.slice(0, 16)?.replace("T", " ") ?? "unknown"} — NOT live`
                    : "positions are live"}
                  ; SMA windows are frozen — §6.3 forbids retuning)
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Contract</TableHead>
                    <TableHead>Pos</TableHead>
                    <TableHead className="text-right">SMA</TableHead>
                    <TableHead className="text-right">Realized ₹</TableHead>
                    <TableHead className="text-right">Trades</TableHead>
                    <TableHead className="text-right">Win rate</TableHead>
                    <TableHead className="text-right">Overshoot</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {instruments.map((i) => (
                    <TableRow key={i.symbol}>
                      <TableCell className="font-medium">{i.symbol}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{i.tradingsymbol || "—"}</TableCell>
                      <TableCell>
                        {stale || i.carried ? (
                          <Badge variant="outline" className="text-amber-700">
                            {i.open_pos > 0 ? "long" : i.open_pos < 0 ? "short" : "flat"} (stale)
                          </Badge>
                        ) : (
                          <PosBadge pos={i.open_pos} />
                        )}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {i.short != null && i.long != null ? `${i.short}/${i.long}` : "—"}
                      </TableCell>
                      <TableCell className={cn("text-right", pnlClass(i.realized_rupees))}>{formatINR(i.realized_rupees)}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">{i.n_trades}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {i.win_rate == null ? "—" : `${(i.win_rate * 100).toFixed(1)}%`}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {i.n_stop_fills > 0 ? formatINR(i.stop_overshoot_rupees) : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                Session trades{" "}
                <span className="text-xs font-normal text-muted-foreground">
                  (latest session{data?.latest_date ? ` ${data.latest_date}` : ""}; net of costs)
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              {instruments.every((i) => (i.session_trades?.length ?? 0) === 0) ? (
                <p className="text-sm text-muted-foreground">No fills in the latest session.</p>
              ) : (
                instruments.map((i) => <SessionBook key={i.symbol} inst={i} />)
              )}
            </CardContent>
          </Card>
        </>
      )}

      {data?.note && (
        <p className="text-xs text-muted-foreground">{data.note}</p>
      )}
    </div>
  );
}
