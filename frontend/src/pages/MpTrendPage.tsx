import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Info } from "lucide-react";
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

const POLL_MS = 60_000;
const WINDOW_DAYS = 60;

function pnlClass(v: number): string {
  return cn("tabular-nums", v > 0 && "text-emerald-600", v < 0 && "text-rose-600");
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      </CardContent>
    </Card>
  );
}

function PnlValue({ value }: { value: number }) {
  return <span className={pnlClass(value)}>{formatINR(value, 0)}</span>;
}

export function MpTrendPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["mp-trend", WINDOW_DAYS],
    queryFn: () => api.mpTrend({ days: WINDOW_DAYS }),
    refetchInterval: POLL_MS,
  });

  const chartData = useMemo(
    () =>
      (data?.daily ?? []).map((d) => ({
        date: new Date(d.date).toLocaleDateString("en-IN", {
          day: "2-digit",
          month: "short",
        }),
        cumulative: d.cum_net,
      })),
    [data],
  );

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="rounded-md border border-border bg-card p-6 text-sm text-muted-foreground">
        Could not load the Market-Profile trend book. The paper runner may not
        have produced any rows yet (<code>runners/run_paper_mp.py</code>).
      </div>
    );
  }

  const s = data.summary;
  const hasData = data.daily.length > 0;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">
            Market Profile — Trend-Up Overnight Continuation
          </h1>
          <p className="text-sm text-muted-foreground">
            Long every <code>trend_up</code> name at the close on broad-momentum
            days (≥3 signals), exit next close · paper
            {s.latest_date ? ` · latest ${s.latest_date}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {s.halted ? (
            <Badge variant="destructive">HALTED</Badge>
          ) : (
            <Badge variant="outline">running · paper</Badge>
          )}
        </div>
      </div>

      {!hasData && (
        <div className="flex items-start gap-2 rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            No paper runs yet. Once <code>runners/run_paper_mp.py</code> processes a
            trading day it writes to <code>mp_trend_runs</code> /{" "}
            <code>mp_trend_positions</code>, which this page reads.
          </span>
        </div>
      )}

      {s.halted && s.halt_reason && (
        <div className="flex items-start gap-2 rounded-md border border-rose-300 bg-rose-50 p-4 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Kill switch tripped — new entries halted. Reason: {s.halt_reason}.
            Open positions still exit normally.
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="Net P&L" value={formatINR(s.net_pnl, 0)} />
        <MetricCard label="Gross" value={formatINR(s.gross_pnl, 0)} />
        <MetricCard label="Costs" value={formatINR(s.costs, 0)} />
        <MetricCard label="Win rate" value={`${formatNum(s.win_rate * 100, 1)}%`} />
        <MetricCard label="Open" value={String(s.n_open_positions)} />
        <MetricCard label="Closed trades" value={String(s.n_closed_trades)} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Cumulative Net P&L</CardTitle>
        </CardHeader>
        <CardContent className="h-72">
          {chartData.length === 0 ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              No daily P&L yet.
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 10, right: 10, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="mp-trend-pnl" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.4} />
                    <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="date"
                  tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  interval="preserveStartEnd"
                />
                <YAxis
                  tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  width={64}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "hsl(var(--card))",
                    border: "1px solid hsl(var(--border))",
                    fontSize: 12,
                  }}
                  formatter={(v: number) => `₹${v.toLocaleString("en-IN")}`}
                />
                <Area
                  type="monotone"
                  dataKey="cumulative"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  fill="url(#mp-trend-pnl)"
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Open Positions ({data.open_positions.length}) — held overnight
          </CardTitle>
        </CardHeader>
        <CardContent>
          {data.open_positions.length === 0 ? (
            <div className="py-6 text-center text-sm text-muted-foreground">
              No open positions (flat — no broad-momentum day, or exited).
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Entry date</TableHead>
                  <TableHead className="text-right">Entry</TableHead>
                  <TableHead className="text-right">Qty</TableHead>
                  <TableHead className="text-right">Notional</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.open_positions.map((p) => (
                  <TableRow key={`${p.symbol}-${p.entry_date}`}>
                    <TableCell className="font-medium">{p.symbol}</TableCell>
                    <TableCell>{p.entry_date}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatNum(p.entry_px, 2)}</TableCell>
                    <TableCell className="text-right tabular-nums">{p.qty}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatINR(p.notional, 0)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Daily Runs</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead className="text-right">Trend-up</TableHead>
                <TableHead className="text-right">Opened</TableHead>
                <TableHead className="text-right">Closed</TableHead>
                <TableHead className="text-right">Day P&L</TableHead>
                <TableHead className="text-right">Cumulative</TableHead>
                <TableHead className="text-right">Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[...data.daily].reverse().map((d) => (
                <TableRow key={d.date}>
                  <TableCell>{d.date}</TableCell>
                  <TableCell className="text-right tabular-nums">{d.n_trend_up}</TableCell>
                  <TableCell className="text-right tabular-nums">{d.n_opened}</TableCell>
                  <TableCell className="text-right tabular-nums">{d.n_closed}</TableCell>
                  <TableCell className="text-right">
                    <PnlValue value={d.day_net} />
                  </TableCell>
                  <TableCell className="text-right">
                    <PnlValue value={d.cum_net} />
                  </TableCell>
                  <TableCell className="text-right">
                    {d.halted ? (
                      <Badge variant="destructive">halted</Badge>
                    ) : d.n_opened > 0 ? (
                      <Badge variant="outline">entered</Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">flat</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
