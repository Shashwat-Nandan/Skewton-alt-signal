import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
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
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatINR, formatNum } from "@/lib/utils";
import type { EquityPendingEntry, EquityPosition } from "@/lib/types";

const POLL_MS = 60_000;

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  // Date-only strings (the runner's signal_dt) must be parsed as LOCAL
  // dates: new Date("YYYY-MM-DD") is spec-defined UTC midnight, which
  // renders the PREVIOUS day for viewers west of UTC — making a fresh
  // signal look a day staler against the 5-day skip window.
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  const d = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", { dateStyle: "medium" });
}

function pnlOf(p: EquityPosition): number {
  if (p.status === "CLOSED" && p.pnl != null) return p.pnl;
  const mark = p.last_mtm_px ?? p.entry_px;
  return (mark - p.entry_px) * p.qty;
}

function rMultiple(p: EquityPosition): number | null {
  const risk = (p.entry_px - p.initial_sl) * p.qty;
  if (risk <= 0) return null;
  return pnlOf(p) / risk;
}

function PnlCell({ value }: { value: number }) {
  return (
    <span
      className={cn(
        "tabular-nums",
        value > 0 && "text-emerald-600",
        value < 0 && "text-rose-600",
      )}
    >
      {formatINR(value, 0)}
    </span>
  );
}

function PositionsTable({
  rows,
  isLoading,
  emptyMessage,
  onSelect,
}: {
  rows: EquityPosition[];
  isLoading: boolean;
  emptyMessage: string;
  onSelect: (p: EquityPosition) => void;
}) {
  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10" />
        <Skeleton className="h-10" />
        <Skeleton className="h-10" />
      </div>
    );
  }
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Symbol</TableHead>
          <TableHead>Entry</TableHead>
          <TableHead className="text-right">Qty</TableHead>
          <TableHead className="text-right">Entry ₹</TableHead>
          <TableHead className="text-right">SL ₹</TableHead>
          <TableHead className="text-right">Target ₹</TableHead>
          <TableHead className="text-right">Mark ₹</TableHead>
          <TableHead className="text-right">P&amp;L</TableHead>
          <TableHead className="text-right">R</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((p) => {
          const pnl = pnlOf(p);
          const r = rMultiple(p);
          const mark = p.status === "CLOSED" ? p.exit_px : p.last_mtm_px;
          return (
            <TableRow
              key={p.id}
              className="cursor-pointer"
              onClick={() => onSelect(p)}
            >
              <TableCell className="font-medium">{p.symbol}</TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {formatDate(p.entry_dt)}
              </TableCell>
              <TableCell className="text-right tabular-nums">{p.qty}</TableCell>
              <TableCell className="text-right tabular-nums">
                {formatNum(p.entry_px, 2)}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {formatNum(p.current_sl, 2)}
                {p.current_sl !== p.initial_sl && (
                  <span className="ml-1 text-[10px] text-muted-foreground">
                    (init {formatNum(p.initial_sl, 2)})
                  </span>
                )}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {formatNum(p.target, 2)}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {mark == null ? "—" : formatNum(mark, 2)}
              </TableCell>
              <TableCell className="text-right">
                <PnlCell value={pnl} />
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {r == null ? "—" : `${formatNum(r, 2)}R`}
              </TableCell>
              <TableCell>
                {p.status === "OPEN" ? (
                  <Badge variant="outline">OPEN</Badge>
                ) : (
                  <Badge variant="secondary">{p.exit_reason ?? "CLOSED"}</Badge>
                )}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function PositionDetail({
  p,
  onClose,
}: {
  p: EquityPosition;
  onClose: () => void;
}) {
  const pnl = pnlOf(p);
  const r = rMultiple(p);
  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-end bg-background/40 backdrop-blur-sm sm:items-center sm:justify-end"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md bg-card border-l border-border h-full overflow-y-auto p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold">{p.symbol}</h2>
            <p className="text-xs text-muted-foreground">
              {p.side} · entered {formatTimestamp(p.entry_dt)}
            </p>
          </div>
          <button
            className="text-muted-foreground hover:text-foreground"
            onClick={onClose}
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
          <div>
            <div className="text-xs text-muted-foreground">Quantity</div>
            <div className="tabular-nums">{p.qty}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Status</div>
            <div>
              {p.status === "OPEN"
                ? "Open"
                : `Closed (${p.exit_reason ?? "manual"})`}
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Entry price</div>
            <div className="tabular-nums">{formatINR(p.entry_px, 2)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">
              {p.status === "OPEN" ? "Mark price" : "Exit price"}
            </div>
            <div className="tabular-nums">
              {(() => {
                // null exit_px must read as missing data ("—"), never as a
                // fabricated ₹0.00 exit.
                const v =
                  p.status === "OPEN" ? p.last_mtm_px ?? p.entry_px : p.exit_px;
                return v == null ? "—" : formatINR(v, 2);
              })()}
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Initial SL</div>
            <div className="tabular-nums">{formatINR(p.initial_sl, 2)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Current SL</div>
            <div className="tabular-nums">{formatINR(p.current_sl, 2)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Target</div>
            <div className="tabular-nums">{formatINR(p.target, 2)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">High watermark</div>
            <div className="tabular-nums">
              {p.high_watermark == null ? "—" : formatINR(p.high_watermark, 2)}
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">ATR at entry</div>
            <div className="tabular-nums">{formatNum(p.atr_at_entry, 2)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Opened by</div>
            <div>{p.opened_by_scan ?? "—"} scan</div>
          </div>
          <div className="col-span-2 border-t border-border pt-3">
            <div className="text-xs text-muted-foreground">P&amp;L</div>
            <div className="text-lg font-semibold">
              <PnlCell value={pnl} />
              {r != null && (
                <span className="ml-2 text-sm text-muted-foreground tabular-nums">
                  ({formatNum(r, 2)}R)
                </span>
              )}
            </div>
          </div>
        </div>

        {p.rationale && (
          <div className="mt-4">
            <div className="text-xs text-muted-foreground">Rationale</div>
            <pre className="mt-1 whitespace-pre-wrap break-words rounded-md bg-muted px-3 py-2 text-xs">
              {p.rationale}
            </pre>
          </div>
        )}

        {p.exit_dt && (
          <div className="mt-4 text-xs text-muted-foreground">
            Exited {formatTimestamp(p.exit_dt)}
          </div>
        )}
      </div>
    </div>
  );
}

// Backend hard cap for /delivery/positions. Request it explicitly: the
// 500-row default silently dropped the OLDEST closed rows (OPEN-first,
// newest-first ordering), understating the "lifetime" card.
const POSITIONS_LIMIT = 2000;

function QueryErrorNote({ error, what }: { error: unknown; what: string }) {
  return (
    <p className="text-sm text-destructive">
      Failed to load {what}:{" "}
      {error instanceof Error ? error.message : "request failed"} — this is an
      API error, not an empty book.
    </p>
  );
}

export function DeliveryAccumPage() {
  const [selected, setSelected] = useState<EquityPosition | null>(null);

  const positions = useQuery({
    queryKey: ["delivery", "positions"],
    queryFn: () => api.deliveryPositions(undefined, POSITIONS_LIMIT),
    refetchInterval: POLL_MS,
  });

  const scans = useQuery({
    queryKey: ["delivery", "scans"],
    queryFn: () => api.deliveryScans(20),
    refetchInterval: POLL_MS,
  });

  const signals = useQuery({
    queryKey: ["delivery", "signals"],
    queryFn: () => api.deliverySignals(),
    refetchInterval: POLL_MS,
  });

  const pendingEntries = useQuery({
    queryKey: ["delivery", "pending-entries", "PENDING"],
    queryFn: () => api.deliveryPendingEntries("PENDING"),
    refetchInterval: POLL_MS,
  });

  const allPositions = positions.data?.positions ?? [];
  const openPositions = useMemo(
    () => allPositions.filter((p) => p.status === "OPEN"),
    [allPositions],
  );
  const closedPositions = useMemo(
    () => allPositions.filter((p) => p.status === "CLOSED"),
    [allPositions],
  );

  const lastScan = scans.data?.scans?.[0];
  const totalOpenPnl = openPositions.reduce((acc, p) => acc + pnlOf(p), 0);
  const totalClosedPnl = closedPositions.reduce((acc, p) => acc + pnlOf(p), 0);
  const winCount = closedPositions.filter((p) => pnlOf(p) > 0).length;
  const winRate = closedPositions.length
    ? winCount / closedPositions.length
    : null;
  const nPending = pendingEntries.data?.pending.length ?? 0;
  // Cards must never show authoritative zeros for data that isn't there:
  // loading → skeleton, error → em-dash (code-review 2026-07-23).
  const posReady = positions.data != null;
  const positionsTruncated = allPositions.length >= POSITIONS_LIMIT;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Delivery Accumulation</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Varsity &ldquo;who&rsquo;s holding&rdquo; — long positional entries on
          clustered own-history delivery-percentile extremes while price sits
          near yearly lows. Paper-only; entries fill at next-day open.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Open positions
            </CardTitle>
          </CardHeader>
          <CardContent>
            {!posReady ? (
              positions.error ? (
                <div className="text-2xl font-semibold text-destructive">—</div>
              ) : (
                <Skeleton className="h-8 w-16" />
              )
            ) : (
              <>
                <div className="text-2xl font-semibold tabular-nums">
                  {openPositions.length}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  Unrealised <PnlCell value={totalOpenPnl} />
                </div>
              </>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Closed ({positionsTruncated ? `latest ${POSITIONS_LIMIT}` : "lifetime"})
            </CardTitle>
          </CardHeader>
          <CardContent>
            {!posReady ? (
              positions.error ? (
                <div className="text-2xl font-semibold text-destructive">—</div>
              ) : (
                <Skeleton className="h-8 w-16" />
              )
            ) : (
              <>
                <div className="text-2xl font-semibold tabular-nums">
                  {closedPositions.length}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  Realised <PnlCell value={totalClosedPnl} />
                  {winRate != null && (
                    <span className="ml-2">
                      · win-rate {formatNum(winRate * 100, 1)}%
                    </span>
                  )}
                  {positionsTruncated && (
                    <span className="ml-2 text-amber-600">
                      · truncated at {POSITIONS_LIMIT} rows
                    </span>
                  )}
                </div>
              </>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Last scan
            </CardTitle>
          </CardHeader>
          <CardContent>
            {scans.error ? (
              <div className="text-xs text-destructive">
                API error — scan status unknown
              </div>
            ) : scans.isLoading ? (
              <Skeleton className="h-8 w-24" />
            ) : (
              <>
                <div className="text-sm font-medium">
                  {lastScan ? (
                    <>
                      {lastScan.scan_kind}{" "}
                      <span className="text-muted-foreground">·</span>{" "}
                      {lastScan.mode}
                    </>
                  ) : (
                    "—"
                  )}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {lastScan ? formatTimestamp(lastScan.scan_dt) : "no scans yet"}
                  {lastScan && (
                    <>
                      {" · "}signals {lastScan.n_signals} · trades{" "}
                      {lastScan.n_trades}
                    </>
                  )}
                </div>
              </>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Queued for next open
            </CardTitle>
          </CardHeader>
          <CardContent>
            {pendingEntries.error ? (
              <div className="text-xs text-destructive">
                API error — queue unknown
              </div>
            ) : pendingEntries.isLoading ? (
              <Skeleton className="h-8 w-16" />
            ) : (
              <>
                <div className="text-2xl font-semibold tabular-nums">
                  {nPending}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  fill at next session&rsquo;s open (18:45 close scan)
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {positions.error ? (
        <Card>
          <CardContent className="pt-6 text-sm text-destructive">
            {positions.error instanceof Error
              ? positions.error.message
              : "Failed to load positions."}
          </CardContent>
        </Card>
      ) : (
        <Tabs defaultValue="open">
          <TabsList>
            <TabsTrigger value="open">Open ({openPositions.length})</TabsTrigger>
            <TabsTrigger value="closed">
              Closed ({closedPositions.length})
            </TabsTrigger>
            <TabsTrigger value="signals">
              Today's signals
              {signals.data?.signals.length
                ? ` (${signals.data.signals.length})`
                : ""}
            </TabsTrigger>
            <TabsTrigger value="pending">
              Pending fills
              {nPending ? ` (${nPending})` : ""}
            </TabsTrigger>
            <TabsTrigger value="scans">Scans</TabsTrigger>
          </TabsList>
          <TabsContent value="open">
            <Card>
              <CardContent className="pt-6">
                <PositionsTable
                  rows={openPositions}
                  isLoading={positions.isLoading}
                  emptyMessage="No open positions. The strategy is flat."
                  onSelect={setSelected}
                />
              </CardContent>
            </Card>
          </TabsContent>
          <TabsContent value="closed">
            <Card>
              <CardContent className="pt-6">
                <PositionsTable
                  rows={closedPositions}
                  isLoading={positions.isLoading}
                  emptyMessage="No closed positions yet."
                  onSelect={setSelected}
                />
              </CardContent>
            </Card>
          </TabsContent>
          <TabsContent value="signals">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">
                  Signals for {signals.data?.date ?? "today"}
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                  JSONL emissions from the latest delivery-accum scan
                  (signals-mode tail).
                </p>
              </CardHeader>
              <CardContent>
                {signals.error ? (
                  <QueryErrorNote error={signals.error} what="signals" />
                ) : signals.isLoading ? (
                  <Skeleton className="h-20" />
                ) : !signals.data?.signals.length ? (
                  <p className="text-sm text-muted-foreground">
                    No signals emitted for this date.
                  </p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Time</TableHead>
                        <TableHead>Symbol</TableHead>
                        <TableHead>Side</TableHead>
                        <TableHead className="text-right">Qty</TableHead>
                        <TableHead className="text-right">Price</TableHead>
                        <TableHead>Rationale</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {signals.data.signals.map((s, i) => (
                        <TableRow key={`${s.timestamp}-${s.tradingsymbol}-${i}`}>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatTimestamp(s.timestamp)}
                          </TableCell>
                          <TableCell className="font-medium">
                            {s.tradingsymbol}
                          </TableCell>
                          <TableCell>
                            <Badge
                              variant={
                                s.transaction_type === "BUY"
                                  ? "default"
                                  : "secondary"
                              }
                            >
                              {s.transaction_type}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {s.quantity}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatNum(s.price, 2)}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {s.rationale ?? "—"}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>
          <TabsContent value="pending">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">
                  Pending fills
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                  Close-scan signals queued for the next session&rsquo;s open.
                  Each row fills at next-day open with SL / target re-anchored
                  to the actual fill, OR is skipped if the gap exceeds 1.5×ATR
                  (SKIPPED_GAP) or the signal ages past 5 days (SKIPPED_STALE).
                </p>
              </CardHeader>
              <CardContent>
                {pendingEntries.error ? (
                  <QueryErrorNote
                    error={pendingEntries.error}
                    what="pending entries"
                  />
                ) : pendingEntries.isLoading ? (
                  <Skeleton className="h-20" />
                ) : !pendingEntries.data?.pending.length ? (
                  <p className="text-sm text-muted-foreground">
                    No signals queued for the next-day open.
                  </p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Signal date</TableHead>
                        <TableHead>Symbol</TableHead>
                        <TableHead>Side</TableHead>
                        <TableHead className="text-right">Qty</TableHead>
                        <TableHead className="text-right">Signal close</TableHead>
                        <TableHead className="text-right">SL dist</TableHead>
                        <TableHead className="text-right">Tgt dist</TableHead>
                        <TableHead className="text-right">ATR</TableHead>
                        <TableHead>Rationale</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {pendingEntries.data.pending.map((p: EquityPendingEntry) => (
                        <TableRow key={p.id}>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatDate(p.signal_dt)}
                          </TableCell>
                          <TableCell className="font-medium">
                            {p.symbol}
                          </TableCell>
                          <TableCell>
                            <Badge variant="default">{p.side}</Badge>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {p.qty}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatNum(p.signal_close, 2)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatNum(p.sl_distance, 2)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatNum(p.target_distance, 2)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatNum(p.atr, 2)}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {p.rationale ?? "—"}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>
          <TabsContent value="scans">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Recent scans</CardTitle>
              </CardHeader>
              <CardContent>
                {scans.error ? (
                  <QueryErrorNote error={scans.error} what="scan history" />
                ) : scans.isLoading ? (
                  <Skeleton className="h-20" />
                ) : !scans.data?.scans.length ? (
                  <p className="text-sm text-muted-foreground">
                    No scans recorded yet — cron timers may not have fired.
                  </p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>When</TableHead>
                        <TableHead>Kind</TableHead>
                        <TableHead>Mode</TableHead>
                        <TableHead className="text-right">Signals</TableHead>
                        <TableHead className="text-right">Trades</TableHead>
                        <TableHead className="text-right">Open</TableHead>
                        <TableHead className="text-right">Closed today</TableHead>
                        <TableHead>Notes</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {scans.data.scans.map((s) => (
                        <TableRow key={s.id}>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatTimestamp(s.scan_dt)}
                          </TableCell>
                          <TableCell>{s.scan_kind}</TableCell>
                          <TableCell>{s.mode}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {s.n_signals}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {s.n_trades}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {s.n_open_positions}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {s.n_closed_today}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {s.notes ?? "—"}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      )}

      {selected && (
        <PositionDetail p={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
