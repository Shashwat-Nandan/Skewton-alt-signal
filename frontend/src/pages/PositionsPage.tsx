import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownRight,
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Pause,
  RefreshCw,
  Wallet,
} from "lucide-react";
import { api } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatINR, formatNum, formatTime, isMarketHoursIST } from "@/lib/utils";
import type {
  ClosedTrade,
  OpenPosition,
  PositionSystemBlock,
  PositionsResponse,
} from "@/lib/types";

const REFRESH_MS = 10_000;

// ───────────────── helpers ─────────────────

function pnlClass(n: number | null | undefined): string {
  if (n == null || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-emerald-500 dark:text-emerald-400" : "text-rose-500 dark:text-rose-400";
}

function pnlGradient(n: number | null | undefined): string {
  if (n == null || n === 0) return "from-muted/40 to-transparent";
  return n > 0
    ? "from-emerald-500/20 via-emerald-500/5 to-transparent"
    : "from-rose-500/20 via-rose-500/5 to-transparent";
}

function pctChange(entry: number, current: number | null | undefined): number | null {
  if (current == null || entry === 0) return null;
  return ((current - entry) / entry) * 100;
}

/** Track previous prop value to detect a change between polls. */
function usePrev<T>(value: T): T | undefined {
  const ref = useRef<T | undefined>(undefined);
  useEffect(() => {
    ref.current = value;
  }, [value]);
  return ref.current;
}

/** Briefly apply flash-up/down when `value` changes vs the previous render. */
function FlashOnChange({
  value,
  children,
  className,
}: {
  value: number | null | undefined;
  children: React.ReactNode;
  className?: string;
}) {
  const prev = usePrev(value);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);

  useEffect(() => {
    if (prev == null || value == null || prev === value) return;
    setFlash(value > prev ? "up" : "down");
    const t = setTimeout(() => setFlash(null), 1200);
    return () => clearTimeout(t);
  }, [value, prev]);

  return (
    <span
      className={cn(
        "inline-block rounded px-1 transition-colors",
        flash === "up" && "flash-up",
        flash === "down" && "flash-down",
        className,
      )}
    >
      {children}
    </span>
  );
}

// ───────────────── small atoms ─────────────────

function ModeBadge({ mode }: { mode: "paper" | "live" }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "uppercase tracking-wide font-semibold",
        mode === "live"
          ? "border-rose-500/60 bg-rose-500/10 text-rose-600 dark:text-rose-400"
          : "border-amber-500/60 bg-amber-500/10 text-amber-600 dark:text-amber-400",
      )}
    >
      {mode}
    </Badge>
  );
}

function SideBadge({ side }: { side: "LONG" | "SHORT" }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "tabular-nums font-semibold",
        side === "LONG"
          ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          : "border-rose-500/60 bg-rose-500/10 text-rose-600 dark:text-rose-400",
      )}
    >
      {side === "LONG" ? <ArrowUpRight className="mr-1 h-3 w-3" /> : <ArrowDownRight className="mr-1 h-3 w-3" />}
      {side}
    </Badge>
  );
}

function LiveDot({ active }: { active: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex h-2 w-2 rounded-full",
        active
          ? "bg-emerald-500 animate-live-pulse"
          : "bg-muted-foreground/50",
      )}
      aria-label={active ? "polling" : "idle"}
    />
  );
}

/** Horizontal bar showing this position's |P&L| as a share of `max`. */
function PnlIntensityBar({
  pnl,
  max,
}: {
  pnl: number | null | undefined;
  max: number;
}) {
  if (pnl == null || max === 0) return <div className="h-1 w-full rounded bg-muted/40" />;
  const pct = Math.min(100, (Math.abs(pnl) / max) * 100);
  return (
    <div className="h-1 w-full overflow-hidden rounded bg-muted/40">
      <div
        className={cn(
          "h-full rounded transition-all duration-700",
          pnl >= 0 ? "bg-emerald-500/80" : "bg-rose-500/80",
        )}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

// ───────────────── hero ─────────────────

function Hero({ data, marketOpen }: { data: PositionsResponse; marketOpen: boolean }) {
  const totals = data.systems.reduce(
    (acc, s) => ({
      realized: acc.realized + s.summary.realized_pnl,
      unrealized: acc.unrealized + s.summary.unrealized_pnl,
      total: acc.total + s.summary.total_pnl,
      open: acc.open + s.summary.n_open_positions,
      closed: acc.closed + s.summary.n_closed_today,
      costs: acc.costs + s.summary.transaction_costs,
    }),
    { realized: 0, unrealized: 0, total: 0, open: 0, closed: 0, costs: 0 },
  );
  return (
    <Card
      className={cn(
        "relative overflow-hidden border-border/60 bg-gradient-to-br shadow-lg",
        pnlGradient(totals.total),
      )}
    >
      {/* Subtle backdrop accent */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_right,hsl(var(--primary)/0.08),transparent_60%)]" />
      <CardContent className="relative flex flex-col gap-6 p-6 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs uppercase tracking-widest text-muted-foreground">
            <Wallet className="h-3.5 w-3.5" />
            Net P&L · today
          </div>
          <div
            className={cn(
              "mt-2 text-4xl font-bold tabular-nums tracking-tight sm:text-5xl",
              pnlClass(totals.total),
            )}
          >
            <FlashOnChange value={totals.total}>{formatINR(totals.total)}</FlashOnChange>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span className="text-muted-foreground">
              <span className="font-medium text-foreground">{totals.open}</span> open ·{" "}
              <span className="font-medium text-foreground">{totals.closed}</span> closed today
            </span>
            <span className="text-muted-foreground">·</span>
            <span className={cn("tabular-nums", pnlClass(totals.realized))}>
              realised <FlashOnChange value={totals.realized}>{formatINR(totals.realized)}</FlashOnChange>
            </span>
            <span className="text-muted-foreground">·</span>
            <span className={cn("tabular-nums", pnlClass(totals.unrealized))}>
              unrealised <FlashOnChange value={totals.unrealized}>{formatINR(totals.unrealized)}</FlashOnChange>
            </span>
            <span className="text-muted-foreground">·</span>
            <span className="text-muted-foreground tabular-nums">
              costs {formatINR(-Math.abs(totals.costs))}
            </span>
          </div>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <div className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-card/80 px-3 py-1.5 text-xs backdrop-blur">
            <LiveDot active={marketOpen} />
            <span className="font-medium">{marketOpen ? "NSE Open" : "NSE Closed"}</span>
            <span className="text-muted-foreground">
              {marketOpen ? `· auto-refresh ${REFRESH_MS / 1000}s` : "· manual refresh"}
            </span>
          </div>
          <div className="text-xs text-muted-foreground">
            Snapshot {formatTime(data.generated_at)}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ───────────────── open positions ─────────────────

function OpenPositionsTable({ rows }: { rows: OpenPosition[] }) {
  if (rows.length === 0) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-dashed border-border/60 bg-muted/20 px-4 py-6 text-sm text-muted-foreground">
        <Pause className="h-4 w-4" />
        No open positions in this system right now.
      </div>
    );
  }
  const maxAbs = Math.max(
    1,
    ...rows.map((r) => (r.unrealized_pnl != null ? Math.abs(r.unrealized_pnl) : 0)),
  );

  return (
    <div className="overflow-hidden rounded-lg border border-border/60">
      <Table>
        <TableHeader>
          <TableRow className="bg-muted/30 hover:bg-muted/30">
            <TableHead>Symbol</TableHead>
            <TableHead>Side</TableHead>
            <TableHead className="text-right">Qty</TableHead>
            <TableHead className="text-right">Entry → LTP</TableHead>
            <TableHead className="text-right">Δ</TableHead>
            <TableHead className="text-right">Unrealised</TableHead>
            <TableHead>Since</TableHead>
            <TableHead className="w-[140px]">Intensity</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((p, i) => {
            const prevGroup = i > 0 ? rows[i - 1].group : null;
            const isNewGroup = prevGroup !== p.group;
            const pct = pctChange(p.entry_price, p.current_price);
            return (
              <TableRow
                key={`${p.group}-${p.tradingsymbol}-${i}`}
                className={cn(
                  "transition-colors hover:bg-muted/30",
                  isNewGroup && i > 0 && "border-t-2 border-border",
                )}
              >
                <TableCell>
                  <div className="font-medium leading-tight">{p.tradingsymbol}</div>
                  {isNewGroup && (
                    <div className="mt-0.5 text-xs text-muted-foreground">{p.group}</div>
                  )}
                  {p.note && (
                    <div className="mt-0.5 text-[11px] text-muted-foreground">{p.note}</div>
                  )}
                </TableCell>
                <TableCell>
                  <SideBadge side={p.side} />
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  <span className="font-medium">{p.quantity}</span>
                  <span className="text-muted-foreground"> × {p.lot_size}</span>
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  <div className="text-muted-foreground">{formatNum(p.entry_price)}</div>
                  <div className="font-medium">
                    {p.current_price != null ? (
                      <FlashOnChange value={p.current_price}>
                        {formatNum(p.current_price)}
                      </FlashOnChange>
                    ) : (
                      "—"
                    )}
                  </div>
                </TableCell>
                <TableCell className={cn("text-right tabular-nums", pnlClass(pct))}>
                  {pct == null ? "—" : `${pct >= 0 ? "+" : ""}${formatNum(pct, 2)}%`}
                </TableCell>
                <TableCell
                  className={cn("text-right tabular-nums font-semibold", pnlClass(p.unrealized_pnl))}
                >
                  <FlashOnChange value={p.unrealized_pnl}>
                    {formatINR(p.unrealized_pnl)}
                  </FlashOnChange>
                </TableCell>
                <TableCell className="text-xs tabular-nums text-muted-foreground">
                  {formatTime(p.entry_time)}
                </TableCell>
                <TableCell>
                  <PnlIntensityBar pnl={p.unrealized_pnl} max={maxAbs} />
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

function ClosedTradesTable({ rows }: { rows: ClosedTrade[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="overflow-hidden rounded-lg border border-border/60">
      <Table>
        <TableHeader>
          <TableRow className="bg-muted/30 hover:bg-muted/30">
            <TableHead>Group</TableHead>
            <TableHead>Entry → Exit</TableHead>
            <TableHead className="text-right">Realised</TableHead>
            <TableHead className="text-right">Costs</TableHead>
            <TableHead>Note</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((t, i) => (
            <TableRow key={`${t.group}-${t.exit_time}-${i}`} className="hover:bg-muted/30">
              <TableCell className="font-medium">{t.group}</TableCell>
              <TableCell className="tabular-nums text-muted-foreground">
                {formatTime(t.entry_time)} → {formatTime(t.exit_time)}
              </TableCell>
              <TableCell className={cn("text-right tabular-nums font-semibold", pnlClass(t.realized_pnl))}>
                {formatINR(t.realized_pnl)}
              </TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                {t.transaction_costs != null ? formatINR(-Math.abs(t.transaction_costs)) : "—"}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">{t.note ?? "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ───────────────── system card ─────────────────

function SummaryCell({
  label,
  value,
  colored,
}: {
  label: string;
  value: number;
  colored?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col">
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</span>
      <span
        className={cn(
          "text-sm font-semibold tabular-nums sm:text-base",
          colored && pnlClass(value),
        )}
      >
        <FlashOnChange value={value}>{formatINR(value)}</FlashOnChange>
      </span>
    </div>
  );
}

function SystemCard({ block, index }: { block: PositionSystemBlock; index: number }) {
  const [closedOpen, setClosedOpen] = useState(false);
  const s = block.summary;

  return (
    <Card
      className="animate-fade-in-up relative overflow-hidden border-border/60 transition-shadow hover:shadow-md"
      style={{ animationDelay: `${index * 60}ms` }}
    >
      {/* Left accent bar tied to performance */}
      <div
        className={cn(
          "absolute inset-y-0 left-0 w-1",
          s.total_pnl > 0
            ? "bg-emerald-500"
            : s.total_pnl < 0
            ? "bg-rose-500"
            : "bg-muted-foreground/30",
        )}
      />
      <div className="flex flex-col gap-4 p-5 pl-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold leading-none">{block.label}</h3>
            <ModeBadge mode={block.mode} />
            {!block.available && (
              <Badge variant="outline" className="border-dashed text-muted-foreground">
                <CircleDot className="mr-1 h-3 w-3" />
                not running
              </Badge>
            )}
            <Badge variant="outline" className="font-mono text-[10px] text-muted-foreground">
              {s.n_open_positions} open
            </Badge>
          </div>
          <p className="text-xs text-muted-foreground">
            <span className="font-mono">{block.state_file}</span>
            {block.updated_at && ` · updated ${formatTime(block.updated_at)}`}
          </p>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4 sm:gap-x-8">
          <SummaryCell label="Realised" value={s.realized_pnl} colored />
          <SummaryCell label="Unrealised" value={s.unrealized_pnl} colored />
          <SummaryCell label="Costs" value={-Math.abs(s.transaction_costs)} />
          <div className="flex flex-col">
            <span className="text-[10px] uppercase tracking-widest text-muted-foreground">Net P&L</span>
            <span className={cn("text-lg font-bold tabular-nums sm:text-xl", pnlClass(s.total_pnl))}>
              <FlashOnChange value={s.total_pnl}>{formatINR(s.total_pnl)}</FlashOnChange>
            </span>
          </div>
        </div>
      </div>
      <CardContent className="space-y-3 pl-6">
        <OpenPositionsTable rows={block.open_positions} />
        {block.closed_today.length > 0 && (
          <div className="rounded-lg border border-border/60 bg-muted/10">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-sm font-medium transition-colors hover:bg-muted/30"
              onClick={() => setClosedOpen((v) => !v)}
            >
              <span className="flex items-center gap-2">
                {closedOpen ? (
                  <ChevronDown className="h-4 w-4" />
                ) : (
                  <ChevronRight className="h-4 w-4" />
                )}
                Closed today
                <Badge variant="outline" className="ml-1 font-mono text-[10px]">
                  {block.closed_today.length}
                </Badge>
              </span>
            </button>
            {closedOpen && (
              <div className="border-t border-border/60 p-2">
                <ClosedTradesTable rows={block.closed_today} />
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ───────────────── page ─────────────────

export function PositionsPage() {
  const marketOpen = isMarketHoursIST();
  const { data, isLoading, error, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ["positions"],
    queryFn: api.positions,
    refetchInterval: marketOpen ? REFRESH_MS : false,
    refetchOnWindowFocus: true,
  });

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="bg-gradient-to-br from-foreground to-foreground/60 bg-clip-text text-2xl font-bold tracking-tight text-transparent">
            Positions
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            All paper systems · combined live view
            {dataUpdatedAt > 0 && ` · last fetched ${formatTime(new Date(dataUpdatedAt).toISOString())}`}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
          className="shrink-0"
        >
          <RefreshCw className={cn("h-4 w-4 sm:mr-2", isFetching && "animate-spin")} />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
      </div>

      {error ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive">
              {error instanceof Error ? error.message : "Failed to load positions."}
            </p>
          </CardContent>
        </Card>
      ) : isLoading || !data ? (
        <div className="space-y-4">
          <Skeleton className="h-32" />
          <Skeleton className="h-56" />
          <Skeleton className="h-56" />
        </div>
      ) : (
        <>
          <Hero data={data} marketOpen={marketOpen} />
          <div className="space-y-4">
            {data.systems.map((block, i) => (
              <SystemCard key={block.name} block={block} index={i} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
