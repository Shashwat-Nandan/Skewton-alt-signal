import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

const DAYS = 5;

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

function AggregateRow({ row }: { row: PaperCompareAggregate }) {
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
        {row.n_days_with_data} / {DAYS}
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
  const { data, isLoading, error } = useQuery<PaperCompareResponse>({
    queryKey: ["pair-paper-compare", DAYS],
    queryFn: () => api.pairPaperCompare(DAYS),
    refetchInterval: 5 * 60 * 1000, // EOD JSONs land once per day; 5min poll is generous.
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Baseline vs persistent paper systems</CardTitle>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Head-to-head over the last {DAYS} trading days. P&L is net of transaction costs.
          Persistent admission requires ≥2 of 6 rolling cointegration windows (see
          tasks/todo.md 2026-05-17).
        </p>
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
                    <AggregateRow key={row.system} row={row} />
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
