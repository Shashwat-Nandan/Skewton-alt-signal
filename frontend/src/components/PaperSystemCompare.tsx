import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatINR } from "@/lib/utils";
import type {
  PaperCompareAggregate,
  PaperCompareDailyRow,
  PaperComparePerPair,
  PaperCompareResponse,
} from "@/lib/types";

const DEFAULT_DAYS = 5;
// "kalman" = the Kalman-filter pairs forward A/B test (run_paper_kalman_pairs.py).
// Shows alongside the static baseline/persistent books once that runner is live;
// the compare endpoint renders empty cells for any system without a sidecar yet.
const DEFAULT_SYSTEMS = "baseline,persistent,kalman";

function PnLCell({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-muted-foreground">—</span>;
  return (
    <span
      className={cn(
        "font-mono tabular-nums",
        value > 0 && "text-emerald-600 dark:text-emerald-500",
        value < 0 && "text-rose-600 dark:text-rose-500",
      )}
    >
      {formatINR(value)}
    </span>
  );
}

function AggregateRow({ row, days }: { row: PaperCompareAggregate; days: number }) {
  return (
    <TableRow>
      <TableCell className="font-medium capitalize">{row.system}</TableCell>
      <TableCell className="text-right">
        <PnLCell value={row.net_pnl} />
      </TableCell>
      <TableCell className="text-right">
        <PnLCell value={row.avg_per_day} />
      </TableCell>
      <TableCell className="text-right tabular-nums">{row.n_unique_pairs}</TableCell>
      <TableCell className="text-right tabular-nums">{row.n_closed_trades}</TableCell>
      <TableCell className="text-right tabular-nums">
        {row.n_days_with_data} / {days}
      </TableCell>
    </TableRow>
  );
}

function DailyRowCells({
  row,
  systems,
}: {
  row: PaperCompareDailyRow;
  systems: string[];
}) {
  return (
    <TableRow>
      <TableCell className="font-mono text-xs">{row.date}</TableCell>
      {systems.map((s) => {
        const cell = row.systems[s];
        return (
          <TableCell key={s} className="text-right">
            {cell == null ? (
              <span className="text-xs text-muted-foreground">no EOD file</span>
            ) : (
              <div className="flex flex-col items-end">
                <PnLCell value={cell.net_pnl} />
                <span className="text-xs text-muted-foreground">
                  {cell.n_pairs}p · {cell.n_trades}t
                </span>
              </div>
            )}
          </TableCell>
        );
      })}
    </TableRow>
  );
}

function PerPairTable({
  rows,
  systems,
}: {
  rows: PaperComparePerPair[];
  systems: string[];
}) {
  if (rows.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No pairs traded in this window yet.</p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Pair</TableHead>
          {systems.map((s) => (
            <TableHead key={s} className="text-right capitalize">
              {s}
            </TableHead>
          ))}
          <TableHead>Coverage</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r) => (
          <TableRow key={r.pair}>
            <TableCell className="font-mono text-xs">{r.pair}</TableCell>
            {systems.map((s) => (
              <TableCell key={s} className="text-right">
                <PnLCell value={r.by_system[s]} />
              </TableCell>
            ))}
            <TableCell className="text-xs text-muted-foreground">{r.traded_by}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function PaperSystemCompare() {
  const [daysInput, setDaysInput] = useState<string>(String(DEFAULT_DAYS));
  const [endInput, setEndInput] = useState<string>("");
  const [systemsInput, setSystemsInput] = useState<string>(DEFAULT_SYSTEMS);

  // Validated values fed into the query. `daysInput` may be intermediate
  // during typing (empty or "0"); fall back to the default so the query
  // doesn't spam 422s on every keystroke.
  const parsedDays = Number.parseInt(daysInput, 10);
  const days = Number.isFinite(parsedDays) && parsedDays >= 1 && parsedDays <= 30
    ? parsedDays
    : DEFAULT_DAYS;
  const end = endInput || undefined;
  const systems = systemsInput || DEFAULT_SYSTEMS;

  const { data, isLoading, error } = useQuery<PaperCompareResponse>({
    queryKey: ["pair-paper-compare", days, end, systems],
    queryFn: () => api.pairPaperCompare({ days, end, systems }),
    refetchInterval: 5 * 60 * 1000, // EOD JSONs land once per day; 5min poll is generous.
  });

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-end justify-between gap-3 pb-3">
        <div>
          <CardTitle className="text-base">Paper pair systems — head-to-head</CardTitle>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Baseline vs persistent (static β) vs kalman (time-varying γ — the forward
            A/B test) over the last {days} trading days. P&amp;L is net of transaction
            costs; a system with no sidecar for a day shows an empty cell.
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="paper-compare-days" className="text-xs">Days</Label>
            <Input
              id="paper-compare-days"
              type="number"
              min="1"
              max="30"
              step="1"
              value={daysInput}
              onChange={(e) => setDaysInput(e.target.value)}
              className="h-8 w-20"
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="paper-compare-end" className="text-xs">End date</Label>
            <Input
              id="paper-compare-end"
              type="date"
              value={endInput}
              onChange={(e) => setEndInput(e.target.value)}
              className="h-8 w-36"
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="paper-compare-systems" className="text-xs">Systems</Label>
            <Input
              id="paper-compare-systems"
              type="text"
              value={systemsInput}
              onChange={(e) => setSystemsInput(e.target.value)}
              placeholder="baseline,persistent"
              className="h-8 w-56 font-mono text-xs"
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading && (
          <div className="space-y-2">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-3/4" />
          </div>
        )}
        {error && (
          <p className="text-sm text-rose-600">
            Failed to load comparison: {(error as Error).message}
          </p>
        )}
        {data && (
          <>
            <div>
              <h3 className="mb-1 text-sm font-medium">Aggregate ({data.start_date} → {data.end_date})</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>System</TableHead>
                    <TableHead className="text-right">Net P&amp;L</TableHead>
                    <TableHead className="text-right">Avg / day</TableHead>
                    <TableHead className="text-right">Pairs</TableHead>
                    <TableHead className="text-right">Trades</TableHead>
                    <TableHead className="text-right">Days w/ data</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.aggregate.map((row) => (
                    <AggregateRow key={row.system} row={row} days={days} />
                  ))}
                </TableBody>
              </Table>
            </div>

            <div>
              <h3 className="mb-1 text-sm font-medium">Per-day net P&amp;L</h3>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Date</TableHead>
                    {data.systems.map((s) => (
                      <TableHead key={s} className="text-right capitalize">
                        {s}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.daily.map((row) => (
                    <DailyRowCells key={row.date} row={row} systems={data.systems} />
                  ))}
                </TableBody>
              </Table>
            </div>

            <div>
              <h3 className="mb-1 text-sm font-medium">Per-pair cumulative P&amp;L</h3>
              <PerPairTable rows={data.per_pair} systems={data.systems} />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
