import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Layers, RefreshCw, Wifi, WifiOff } from "lucide-react";
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
import { cn, formatINR, formatNum, isMarketHoursIST } from "@/lib/utils";
import type { UnderlyingExposure } from "@/lib/types";

const REFRESH_MS = 10_000;

function deltaClass(n: number | null | undefined): string {
  if (n == null || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-emerald-500 dark:text-emerald-400" : "text-rose-500 dark:text-rose-400";
}

/** Total delta unknown (null) is a real state, not zero — show it as such
 * so a blank never reads as "flat". */
function deltaCell(n: number | null | undefined) {
  if (n == null) return <span className="text-muted-foreground">—</span>;
  return <span className={deltaClass(n)}>{formatNum(n, 1)}</span>;
}

export function PortfolioPage() {
  // Gate polling to market hours (mirrors PositionsPage): each poll hits the
  // backend's live Kite calls (ltp + positions), so a tab left open overnight
  // must NOT hammer the API off-hours.
  const marketOpen = isMarketHoursIST();
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["portfolio-exposure"],
    queryFn: api.portfolioExposure,
    refetchInterval: marketOpen ? REFRESH_MS : false,
  });

  const shared = (data?.underlyings ?? []).filter((u) => u.shared);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <Layers className="h-6 w-6" /> Portfolio
          </h1>
          <p className="text-sm text-muted-foreground">
            Net exposure per underlying, aggregated across every strategy.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
          aria-label="Refresh"
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
        </Button>
      </div>

      {/* live vs offline banner — the note is the source of truth on whether
          option delta + broker truth actually joined this view. */}
      {data && (
        <div
          className={cn(
            "flex items-center gap-2 rounded-md border px-3 py-2 text-sm",
            data.live
              ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-600 dark:text-emerald-400"
              : "border-amber-500/30 bg-amber-500/5 text-amber-600 dark:text-amber-400",
          )}
        >
          {data.live ? <Wifi className="h-4 w-4" /> : <WifiOff className="h-4 w-4" />}
          <span>{data.note}</span>
        </div>
      )}

      {shared.length > 0 && (
        <div className="flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-sm text-amber-600 dark:text-amber-400">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>
            {shared.length} underlying(s) held by &gt;1 strategy (net-exposure /
            margin overlap): {shared.map((u) => u.underlying).join(", ")}
          </span>
        </div>
      )}

      <Card>
        <CardContent className="p-0 overflow-x-auto">
          {/* Hard error only when we have NOTHING to show. react-query keeps
              the last-good data across a transient poll failure, so we keep
              rendering it (RefreshCw signals the retry) rather than flashing an
              error card that contradicts the still-green live banner above. */}
          {isError && !data ? (
            <p className="p-4 text-sm text-rose-500">Failed to load portfolio exposure.</p>
          ) : isLoading || !data ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </div>
          ) : data.underlyings.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">
              No open positions across any strategy.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Underlying</TableHead>
                  <TableHead className="text-right">Net Δ1</TableHead>
                  <TableHead className="text-right">Option Δ</TableHead>
                  <TableHead className="text-right">Net Δ</TableHead>
                  <TableHead className="text-right">Notional</TableHead>
                  <TableHead>Strategies</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.underlyings.map((u: UnderlyingExposure) => (
                  <TableRow key={u.underlying}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        {u.underlying}
                        {u.shared && (
                          <Badge variant="outline" className="border-amber-500/40 text-amber-600 dark:text-amber-400">
                            shared
                          </Badge>
                        )}
                        {u.has_options && (
                          <Badge variant="outline" className="text-muted-foreground">
                            opt
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {deltaCell(u.net_delta1_units)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {deltaCell(u.net_option_delta)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums font-medium">
                      {deltaCell(u.net_total_delta)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatINR(u.net_notional)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {u.systems.join(", ")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Broker truth — only present when a live session answered. */}
      {data?.broker_net && data.broker_net.length > 0 && (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <div className="px-4 pt-4 text-sm font-medium">Broker net positions (source of truth)</div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Instrument</TableHead>
                  <TableHead>Exchange</TableHead>
                  <TableHead className="text-right">Qty</TableHead>
                  <TableHead className="text-right">Avg price</TableHead>
                  <TableHead className="text-right">P&amp;L</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.broker_net.map((p, i) => (
                  // key by symbol+exchange+index: Kite's net book can list the
                  // same tradingsymbol on two exchanges (NSE/BSE), so the
                  // symbol alone is not unique.
                  <TableRow key={`${p.tradingsymbol}-${p.exchange}-${i}`}>
                    <TableCell className="font-medium">{p.tradingsymbol}</TableCell>
                    <TableCell className="text-muted-foreground">{p.exchange}</TableCell>
                    <TableCell className="text-right tabular-nums">{p.quantity}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatINR(p.average_price)}</TableCell>
                    <TableCell className={cn("text-right tabular-nums", deltaClass(p.pnl))}>
                      {formatINR(p.pnl)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
