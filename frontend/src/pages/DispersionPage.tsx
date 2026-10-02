import { Fragment, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AlertTriangle, Info } from "lucide-react";
import { api } from "@/lib/api";
import type { DispersionBook, DispersionCycle, DispersionSummary } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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

// Books are monthly and decided at the 15:00–15:20 close; a 5-minute poll is plenty.
const POLL_MS = 300_000;

// One colour per book, the same in every chart and table header. Primary blue
// is the theme's; amber reads on both the dark and light backgrounds.
const BOOK_COLOR: Record<string, string> = {
  dispersion_paper: "hsl(var(--primary))",
  dispersion_short_vol_paper: "hsl(38 92% 50%)",
};

function pnlClass(v: number | null | undefined): string {
  return cn("tabular-nums", v != null && v > 0 && "text-emerald-600", v != null && v < 0 && "text-rose-600");
}

function Pnl({ value }: { value: number | null | undefined }) {
  return <span className={pnlClass(value)}>{formatINR(value, 0)}</span>;
}

function Swatch({ name }: { name: string }) {
  return (
    <span
      className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
      style={{ backgroundColor: BOOK_COLOR[name] }}
      aria-hidden
    />
  );
}

function SummaryBlock({ title, s }: { title: string; s: DispersionSummary }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{title}</div>
      {s.cycles === 0 ? (
        <div className="mt-1 text-sm text-muted-foreground">No cycles yet</div>
      ) : (
        <div className="mt-1 space-y-0.5 text-sm">
          <div className="text-2xl font-semibold">
            <Pnl value={s.total_net} />
          </div>
          <div className="text-muted-foreground">
            {s.cycles} cycles · {s.wins} up · worst <Pnl value={s.worst} /> · costs{" "}
            {formatINR(s.costs, 0)}
          </div>
        </div>
      )}
    </div>
  );
}

function BookCard({ book }: { book: DispersionBook }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Swatch name={book.name} />
          {book.label}
        </CardTitle>
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="outline">sizing: {book.sizing}</Badge>
          {book.weightings.map((w) => (
            <Badge key={w} variant="outline">weights: {w}</Badge>
          ))}
          {book.weightings.length > 1 && (
            <Badge variant="destructive">mixed weights in paper record</Badge>
          )}
          {book.replay_weighting && (
            <Badge variant="outline">replay weights: {book.replay_weighting}</Badge>
          )}
          <Badge variant="outline">paper only</Badge>
          {book.open ? (
            <Badge>open · exp {book.open.expiry}</Badge>
          ) : (
            <Badge variant="secondary">flat</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {book.state_error ? (
          <div className="flex items-start gap-2 text-sm text-rose-600 sm:col-span-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>State file could not be read: {book.state_error}</span>
          </div>
        ) : (
          <SummaryBlock title="Paper (forward)" s={book.paper_summary} />
        )}
        <SummaryBlock title="Replay (2-year backtest)" s={book.replay_summary} />
      </CardContent>
    </Card>
  );
}

type Point = { expiry: string } & Record<string, number | string | null>;

function cumulativeSeries(books: DispersionBook[], pick: (b: DispersionBook) => DispersionCycle[]) {
  const byExpiry = new Map<string, Point>();
  for (const b of books) {
    for (const c of pick(b)) {
      const row: Point = byExpiry.get(c.expiry) ?? { expiry: c.expiry };
      row[b.name] = c.cumulative;
      byExpiry.set(c.expiry, row);
    }
  }
  return [...byExpiry.values()].sort((a, b) => a.expiry.localeCompare(b.expiry));
}

function CumulativeChart({ books, data }: { books: DispersionBook[]; data: Point[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-72 items-center justify-center text-sm text-muted-foreground">
        No settled cycles yet.
      </div>
    );
  }
  return (
    <div className="h-72">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="expiry"
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
            tickFormatter={(d: string) =>
              new Date(d).toLocaleDateString("en-IN", { month: "short", year: "2-digit" })
            }
            axisLine={false}
            tickLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
            tickFormatter={(v: number) => `₹${(v / 1e5).toFixed(0)}L`}
            axisLine={false}
            tickLine={false}
            width={56}
          />
          <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeOpacity={0.5} />
          <Tooltip
            contentStyle={{
              backgroundColor: "hsl(var(--card))",
              border: "1px solid hsl(var(--border))",
              fontSize: 12,
            }}
            labelFormatter={(d: string) => `Expiry ${d}`}
            formatter={(v: number, key: string) => [
              formatINR(v, 0),
              books.find((b) => b.name === key)?.label ?? key,
            ]}
          />
          <Legend
            formatter={(key: string) => books.find((b) => b.name === key)?.label ?? key}
            wrapperStyle={{ fontSize: 12 }}
          />
          {books.map((b) => (
            <Line
              key={b.name}
              type="monotone"
              dataKey={b.name}
              stroke={BOOK_COLOR[b.name]}
              strokeWidth={2}
              dot={{ r: 2 }}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function CycleTable({ books, pick }: {
  books: DispersionBook[];
  pick: (b: DispersionBook) => DispersionCycle[];
}) {
  const rows = useMemo(() => {
    const byExpiry = new Map<string, Record<string, DispersionCycle>>();
    for (const b of books) {
      for (const c of pick(b)) {
        const r = byExpiry.get(c.expiry) ?? {};
        r[b.name] = c;
        byExpiry.set(c.expiry, r);
      }
    }
    return [...byExpiry.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  }, [books, pick]);

  if (rows.length === 0) {
    return <div className="py-6 text-center text-sm text-muted-foreground">No cycles yet.</div>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Expiry</TableHead>
          {books.map((b) => (
            <TableHead key={b.name} className="text-right" colSpan={3}>
              <span className="inline-flex items-center gap-1.5">
                <Swatch name={b.name} />
                {b.label}
              </span>
            </TableHead>
          ))}
        </TableRow>
        <TableRow>
          <TableHead />
          {books.map((b) => (
            <Fragment key={b.name}>
              <TableHead className="text-right">Options</TableHead>
              <TableHead className="text-right">Hedge</TableHead>
              <TableHead className="text-right">Net</TableHead>
            </Fragment>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map(([expiry, byBook]) => (
          <TableRow key={expiry}>
            <TableCell className="tabular-nums">{expiry}</TableCell>
            {books.map((b) => {
              const c = byBook[b.name];
              if (!c) {
                return (
                  <TableCell key={b.name} colSpan={3} className="text-center text-xs text-muted-foreground">
                    —
                  </TableCell>
                );
              }
              return (
                <Fragment key={b.name}>
                  <TableCell className="text-right tabular-nums">
                    {formatINR(c.premium_pnl, 0)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatINR(c.futures_pnl, 0)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Pnl value={c.net} />
                    {c.status === "missed_settlement" && (
                      <Badge variant="destructive" className="ml-1.5">missed</Badge>
                    )}
                    {c.settle_basis === "window_ltp_proxy" && (
                      <Badge variant="outline" className="ml-1.5">15:00 proxy</Badge>
                    )}
                  </TableCell>
                </Fragment>
              );
            })}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function OpenBookCard({ book }: { book: DispersionBook }) {
  const ob = book.open;
  if (!ob) return null;
  const legs = [...ob.legs].sort((a, b) => a.side - b.side || a.symbol.localeCompare(b.symbol));
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Swatch name={book.name} />
          Open — {book.label}
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          Expiry {ob.expiry} · entered {ob.entry} · {ob.index_lots} index lots · {ob.n_names} names
          · covered {formatNum(ob.covered_weight * 100, 0)}% of weight · long/short notional{" "}
          {ob.notional_ratio != null ? `${formatNum(ob.notional_ratio, 2)}×` : "—"} · last hedge{" "}
          {ob.last_hedge_session ?? "—"}
        </p>
        <div className="flex flex-wrap gap-4 pt-1 text-sm">
          <span>
            Hedge P&L so far <Pnl value={ob.futures_pnl} />
          </span>
          <span>
            Costs so far <span className="tabular-nums">{formatINR(ob.costs, 0)}</span>
          </span>
        </div>
        <p className="flex items-start gap-1.5 pt-1 text-xs text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          Option legs are carried at entry until expiry settlement; no option mark is shown, so the
          book's P&L is not known until it settles.
        </p>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Underlying</TableHead>
              <TableHead>Side</TableHead>
              <TableHead className="text-right">Strike</TableHead>
              <TableHead className="text-right">Lots × size</TableHead>
              <TableHead className="text-right">Straddle @ entry</TableHead>
              <TableHead className="text-right">IV</TableHead>
              <TableHead className="text-right">Hedge lots</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {legs.map((l) => (
              <TableRow key={l.symbol}>
                <TableCell className="font-medium">{l.symbol}</TableCell>
                <TableCell>
                  <Badge variant={l.side < 0 ? "destructive" : "secondary"}>
                    {l.side < 0 ? "short" : "long"}
                  </Badge>
                </TableCell>
                <TableCell className="text-right tabular-nums">{formatNum(l.strike, 1)}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {l.lots} × {l.lot_size}
                </TableCell>
                <TableCell className="text-right tabular-nums">{formatNum(l.premium, 2)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatNum(l.iv * 100, 1)}%</TableCell>
                <TableCell className="text-right tabular-nums">{l.hedge_lots}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

const pickPaper = (b: DispersionBook) => b.paper;
const pickReplay = (b: DispersionBook) => b.replay;

export function DispersionPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["dispersion-paper"],
    queryFn: api.dispersionPaper,
    refetchInterval: POLL_MS,
  });

  const paperSeries = useMemo(() => cumulativeSeries(data?.books ?? [], pickPaper), [data]);
  const replaySeries = useMemo(() => cumulativeSeries(data?.books ?? [], pickReplay), [data]);

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
        Could not load the dispersion books.
      </div>
    );
  }

  const books = data.books;
  const anyPaper = books.some((b) => b.paper.length > 0 || b.open);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Nifty Dispersion — two paper books</h1>
        <p className="text-sm text-muted-foreground">
          Short the Nifty ATM straddle, long constituent straddles, futures-hedged at the close,
          held to monthly expiry · paper only · per expiry cycle
        </p>
      </div>

      {data.runner_silent_fail && (
        <div className="flex items-start gap-2 rounded-md border border-rose-600/50 bg-rose-600/10 p-4 text-sm text-rose-600">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Runner stopped: every pass in a decision window failed (SILENT_FAIL_dispersion_paper).
            That window's hedge or settlement may not have happened. Investigate the runner and
            quotes, then <code>rm data_cache/SILENT_FAIL_dispersion_paper</code>.
          </span>
        </div>
      )}

      <div className="flex items-start gap-2 rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">
        <Info className="mt-0.5 h-4 w-4 shrink-0" />
        <span>
          {anyPaper ? "" : "No paper cycle yet: both books first enter on the session after the 27 Oct 2026 expiry. "}
          The replay is a daily-bhavcopy sign check (Jul 2024 – Sep 2026) that applies the
          2026-10-01 constituent list (and, for the matched book, the 2026-10-01 free-float
          snapshot) to every cycle, so it carries look-ahead. It is context for the paper
          record, not a promotion result. Paper cycles marked "15:00 proxy" were valued off the
          15:00 last price, not NSE's 15:00–15:30 settlement average.
        </span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {books.map((b) => (
          <BookCard key={b.name} book={b} />
        ))}
      </div>

      <Tabs defaultValue={anyPaper ? "paper" : "replay"}>
        <TabsList>
          <TabsTrigger value="paper">Paper</TabsTrigger>
          <TabsTrigger value="replay">Replay</TabsTrigger>
        </TabsList>
        <TabsContent value="paper" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Cumulative net P&L by expiry — paper</CardTitle>
            </CardHeader>
            <CardContent>
              <CumulativeChart books={books} data={paperSeries} />
            </CardContent>
          </Card>
          {books.map((b) => (
            <OpenBookCard key={b.name} book={b} />
          ))}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Settled cycles — paper</CardTitle>
            </CardHeader>
            <CardContent>
              <CycleTable books={books} pick={pickPaper} />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="replay" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                Cumulative net P&L by expiry — 2-year replay (Book A, hedged, held to expiry)
              </CardTitle>
            </CardHeader>
            <CardContent>
              <CumulativeChart books={books} data={replaySeries} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Replay cycles</CardTitle>
            </CardHeader>
            <CardContent>
              <CycleTable books={books} pick={pickReplay} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
