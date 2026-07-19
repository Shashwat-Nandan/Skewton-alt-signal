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
import type { ArbitrageOpenCalendar } from "@/lib/types";

const POLL_MS = 60_000;
const WINDOW_DAYS = 20;

function pnlClass(v: number): string {
  return cn(
    "tabular-nums",
    v > 0 && "text-emerald-600",
    v < 0 && "text-rose-600",
  );
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

function legSummary(legs: ArbitrageOpenCalendar["legs"]): string {
  if (!legs || legs.length === 0) return "—";
  return legs
    .map((l) => {
      const ts = (l["tradingsymbol"] as string) ?? "?";
      // generate_eod_report() emits the leg quantity under "qty" (not "quantity").
      const qty = (l["qty"] as number) ?? 0;
      const side = qty > 0 ? "+" : "";
      return `${side}${qty} ${ts}`;
    })
    .join("  /  ");
}

export function ArbitragePage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["arbitrage-paper", WINDOW_DAYS],
    queryFn: () => api.arbitragePaper({ days: WINDOW_DAYS }),
    refetchInterval: POLL_MS,
  });

  const chartData = useMemo(
    () =>
      (data?.daily ?? [])
        .filter((d) => d.has_data && d.cumulative_net_pnl != null)
        .map((d) => ({
          date: new Date(d.date).toLocaleDateString("en-IN", {
            day: "2-digit",
            month: "short",
          }),
          cumulative: d.cumulative_net_pnl as number,
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
        Could not load arbitrage P&L. The runner may not have produced an EOD
        sidecar yet (it writes one per trading day).
      </div>
    );
  }

  const s = data.summary;
  const hasData = s.n_days_with_data > 0;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Arbitrage — Calendar Spreads</h1>
          <p className="text-sm text-muted-foreground">
            Term-structure (near vs next-month futures) spreads · paper ·
            {" "}
            {data.start_date} → {data.end_date}
            {s.latest_date ? ` · latest ${s.latest_date}` : ""}
          </p>
        </div>
        <Badge variant="outline">system: {data.system}</Badge>
      </div>

      {!hasData && (
        <div className="flex items-start gap-2 rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            No EOD sidecars found in this window yet. Once
            {" "}
            <code>runners/run_paper_arbitrage.py</code> completes a trading session it
            writes <code>arbitrage_paper_eod_&lt;date&gt;.json</code>, which
            this page reads.
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="Net P&L" value={formatINR(s.net_pnl, 0)} />
        <MetricCard label="Realized" value={formatINR(s.realized_pnl, 0)} />
        <MetricCard label="Unrealized" value={formatINR(s.unrealized_pnl, 0)} />
        <MetricCard label="Costs" value={formatINR(s.transaction_costs, 0)} />
        <MetricCard label="Open spreads" value={String(s.n_open_calendars)} />
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
                  <linearGradient id="arb-pnl" x1="0" y1="0" x2="0" y2="1">
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
                  fill="url(#arb-pnl)"
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Open Calendar Spreads ({data.open_calendars.length})
          </CardTitle>
        </CardHeader>
        <CardContent>
          {data.open_calendars.length === 0 ? (
            <div className="py-6 text-center text-sm text-muted-foreground">
              No open spreads.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Position</TableHead>
                  <TableHead className="text-right">Entry carry diff</TableHead>
                  <TableHead>Legs (signed lots)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.open_calendars.map((c) => (
                  <TableRow key={c.symbol}>
                    <TableCell className="font-medium">{c.symbol}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{c.position}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(c.entry_carry_diff * 100, 2)}%
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {legSummary(c.legs)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Daily P&L</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead className="text-right">Day P&L</TableHead>
                <TableHead className="text-right">Day realized</TableHead>
                <TableHead className="text-right">Closed</TableHead>
                <TableHead className="text-right">Open</TableHead>
                <TableHead className="text-right">Cumulative</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.daily.map((d) => (
                <TableRow key={d.date} className={cn(!d.has_data && "opacity-50")}>
                  <TableCell>{d.date}</TableCell>
                  {d.has_data ? (
                    <>
                      <TableCell className="text-right">
                        <PnlValue value={d.day_pnl ?? 0} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatINR(d.day_realized ?? 0, 0)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {d.n_closed_trades ?? 0}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {d.n_open_calendars ?? 0}
                      </TableCell>
                      <TableCell className="text-right">
                        <PnlValue value={d.cumulative_net_pnl ?? 0} />
                      </TableCell>
                    </>
                  ) : (
                    <TableCell colSpan={5} className="text-center text-xs text-muted-foreground">
                      no session
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
