import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp } from "lucide-react";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { cn, formatNum } from "@/lib/utils";
import { PaperSystemCompare } from "@/components/PaperSystemCompare";
import type { PairCandidate, PairSkipReason } from "@/lib/types";

type PairVariant = "baseline" | "persistent";

type SortKey =
  | "symbol_a"
  | "processing_rank"
  | "latest_z_score"
  | "rank_score"
  | "coint_pvalue"
  | "half_life_days"
  | "correlation"
  | "spread_vol_pct"
  | "hedge_ratio"
  | "persistence_count";

type SortDir = "asc" | "desc";

type Column = { key: SortKey; label: string; align?: "right" | "center"; help?: string };

const BASE_COLUMNS: Column[] = [
  { key: "processing_rank", label: "Order", align: "center", help: "Admit order for the runner under the requested --top cutoff. Blank = skipped (hover for reason)." },
  { key: "symbol_a", label: "Pair" },
  { key: "latest_z_score", label: "Z-score", align: "right", help: "Latest spread vs panel mean/std" },
  { key: "rank_score", label: "Rank", align: "right", help: "Lower is better — composite of p, half-life, vol" },
  { key: "coint_pvalue", label: "Coint p", align: "right", help: "Engle-Granger p-value" },
  { key: "half_life_days", label: "Half-life", align: "right", help: "Days for spread to revert halfway" },
  { key: "correlation", label: "Corr", align: "right", help: "|Pearson corr| of leg prices" },
  { key: "spread_vol_pct", label: "Vol %", align: "right", help: "Spread σ / avg leg price" },
  { key: "hedge_ratio", label: "β", align: "right", help: "OLS hedge ratio (long-leg β · short-leg)" },
];

// Persistent screen surfaces how durable each cointegration is. Inserted right
// after the Pair column so it reads as a headline trust signal.
const PERSISTENCE_COLUMN: Column = {
  key: "persistence_count",
  label: "Windows",
  align: "right",
  help: "Rolling windows the pair cleared cointegration in (higher = more durable). Hover a value for the window indices.",
};

function columnsFor(variant: PairVariant): Column[] {
  if (variant !== "persistent") return BASE_COLUMNS;
  return [BASE_COLUMNS[0], BASE_COLUMNS[1], PERSISTENCE_COLUMN, ...BASE_COLUMNS.slice(2)];
}

const SKIP_REASON_LABEL: Record<PairSkipReason, string> = {
  beta: "|β| outside [0.1, 10] — untradeable hedge ratio",
  quality: "below quality floor (corr < 0.65 OR half-life > 5d OR p > 0.025)",
  leg_cap: "a leg is already at the concentration cap (2× in book)",
  cutoff: "survived filters but ranked below the --top cutoff",
};

function formatScreenedAt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function compareSafe(av: number | string | null, bv: number | string | null, dir: SortDir): number {
  // Nulls always sort to the bottom regardless of direction.
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  if (typeof av === "string" && typeof bv === "string") {
    return dir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
  }
  const an = Number(av);
  const bn = Number(bv);
  return dir === "asc" ? an - bn : bn - an;
}

function sortKeyValue(c: PairCandidate, key: SortKey): number | string | null {
  if (key === "symbol_a") return `${c.symbol_a}/${c.symbol_b}`;
  if (key === "latest_z_score") {
    return c.latest_z_score == null ? null : Math.abs(c.latest_z_score);
  }
  if (key === "processing_rank") return c.processing_rank;
  if (key === "persistence_count") return c.persistence_count;
  return c[key] as number | null;
}

export function PairCandidatesPage({ variant = "baseline" }: { variant?: PairVariant }) {
  const persistent = variant === "persistent";
  const columns = columnsFor(variant);
  const { data, isLoading, error } = useQuery({
    queryKey: ["pair-candidates", variant],
    queryFn: () => (persistent ? api.pairCandidatesPersistent() : api.pairCandidates()),
    refetchInterval: 5 * 60 * 1000, // CSV updates daily; re-poll every 5 min is generous.
  });

  // Default: highest |z-score| first — most actionable rows on top.
  const [sortKey, setSortKey] = useState<SortKey>("latest_z_score");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [minAbsZ, setMinAbsZ] = useState<string>("");
  const [userHasSorted, setUserHasSorted] = useState(false);

  // First-deploy fallback: a CSV produced before this PR has no z-score
  // column. Default-sorting on an all-null column would render with no
  // visible ordering signal, so fall back to rank_score asc.
  useEffect(() => {
    if (userHasSorted || !data?.candidates?.length) return;
    const anyZ = data.candidates.some((c) => c.latest_z_score != null);
    if (!anyZ) {
      setSortKey("rank_score");
      setSortDir("asc");
    }
  }, [data, userHasSorted]);

  const rows = useMemo(() => {
    if (!data?.candidates) return [];
    const minZ = Number(minAbsZ);
    const filtered =
      minAbsZ !== "" && !Number.isNaN(minZ)
        ? data.candidates.filter(
            (c) => c.latest_z_score != null && Math.abs(c.latest_z_score) >= minZ,
          )
        : data.candidates;
    return [...filtered].sort((a, b) =>
      compareSafe(sortKeyValue(a, sortKey), sortKeyValue(b, sortKey), sortDir),
    );
  }, [data, sortKey, sortDir, minAbsZ]);

  function toggleSort(key: SortKey) {
    setUserHasSorted(true);
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Symbol & admit-order sort asc (lower is better/earlier); numeric
      // analytic columns sort desc (most-extreme first).
      setSortDir(key === "symbol_a" || key === "processing_rank" ? "asc" : "desc");
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">
          {persistent ? "Persistent pair candidates" : "Pair candidates"}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {persistent
            ? "Pairs that stayed cointegrated across multiple rolling windows (persistence screen, p≤0.05)."
            : "Cointegrated single-stock-futures pairs from the daily Engle-Granger screen."}
        </p>
      </div>

      {!persistent && <PaperSystemCompare />}

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-end justify-between gap-3 pb-3">
          <div>
            <CardTitle className="text-base">Latest screen</CardTitle>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Generated at {formatScreenedAt(data?.generated_at)}
              {data?.candidates?.[0]?.last_data_date
                ? ` · last bar ${data.candidates[0].last_data_date}`
                : ""}
              {data?.top != null ? ` · runner --top ${data.top}` : ""}
            </p>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="min-abs-z" className="text-xs">
              Min |z-score|
            </Label>
            <Input
              id="min-abs-z"
              type="number"
              inputMode="decimal"
              min="0"
              step="0.1"
              placeholder="any"
              value={minAbsZ}
              onChange={(e) => setMinAbsZ(e.target.value)}
              className="h-8 w-28"
            />
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <p className="text-sm text-destructive">
              {error instanceof Error ? error.message : "Failed to load candidates."}
            </p>
          ) : isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
            </div>
          ) : rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {data?.candidates?.length === 0
                ? "No candidates yet — has screen_pairs.py run?"
                : "No candidates match the current filter."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  {columns.map((col) => {
                    const active = col.key === sortKey;
                    const Arrow = sortDir === "asc" ? ArrowUp : ArrowDown;
                    return (
                      <TableHead
                        key={col.key}
                        className={cn(
                          "cursor-pointer select-none",
                          col.align === "right" && "text-right",
                          col.align === "center" && "text-center",
                        )}
                        onClick={() => toggleSort(col.key)}
                        title={col.help}
                      >
                        <span
                          className={cn(
                            "inline-flex items-center gap-1",
                            active && "text-foreground",
                          )}
                        >
                          {col.label}
                          {active && <Arrow className="h-3 w-3" />}
                        </span>
                      </TableHead>
                    );
                  })}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((c) => (
                  <TableRow
                    key={`${c.symbol_a}-${c.symbol_b}`}
                    className={cn(c.processing_rank == null && "opacity-60")}
                  >
                    <TableCell className="text-center tabular-nums">
                      {c.processing_rank != null ? (
                        <Badge
                          variant="outline"
                          className="border-primary text-primary tabular-nums"
                        >
                          {c.processing_rank}
                        </Badge>
                      ) : (
                        <span
                          className="text-muted-foreground"
                          title={
                            c.skip_reason
                              ? SKIP_REASON_LABEL[c.skip_reason]
                              : "not admitted"
                          }
                        >
                          —
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{c.symbol_a}</span>
                        <span className="text-muted-foreground">/</span>
                        <span className="font-medium">{c.symbol_b}</span>
                      </div>
                      <div className="mt-0.5 text-xs text-muted-foreground tabular-nums">
                        {c.last_close_a != null && c.last_close_b != null
                          ? `${formatNum(c.last_close_a)} / ${formatNum(c.last_close_b)}`
                          : "—"}
                      </div>
                    </TableCell>
                    {persistent && (
                      <TableCell className="text-right tabular-nums">
                        {c.persistence_count == null ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <span
                            title={
                              c.persistence_windows
                                ? `Windows: ${c.persistence_windows}`
                                : undefined
                            }
                          >
                            {c.persistence_count}
                          </span>
                        )}
                      </TableCell>
                    )}
                    <TableCell className="text-right tabular-nums">
                      {c.latest_z_score == null ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        <Badge
                          variant="outline"
                          className={cn(
                            "tabular-nums",
                            Math.abs(c.latest_z_score) >= 2 && "border-primary text-primary",
                          )}
                        >
                          {formatNum(c.latest_z_score)}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(c.rank_score, 3)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(c.coint_pvalue, 4)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {Number.isFinite(c.half_life_days)
                        ? `${formatNum(c.half_life_days, 1)}d`
                        : "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(c.correlation, 3)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(c.spread_vol_pct, 2)}%
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatNum(c.hedge_ratio, 3)}
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
