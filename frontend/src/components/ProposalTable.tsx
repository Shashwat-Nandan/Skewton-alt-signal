import type { ProposalEntry } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { formatTime, formatNum } from "@/lib/utils";

type Props = {
  rows: ProposalEntry[];
  emptyMessage?: string;
  // When set to "pair_trading", legs of the same entry/exit batch are
  // grouped into a single card with explicit BUY/SELL callouts so it's
  // unambiguous which leg to buy and which to sell. Defaults to flat-table
  // rendering for any other (or unset) strategy.
  strategyName?: string;
};

export function ProposalTable({
  rows,
  emptyMessage = "No entries yet.",
  strategyName,
}: Props) {
  if (rows.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  if (strategyName === "pair_trading") {
    return <PairTradeGroupedView rows={rows} />;
  }

  // Newest first
  const ordered = [...rows].reverse();

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-20">Time</TableHead>
          <TableHead className="w-20">Kind</TableHead>
          <TableHead className="w-16">Side</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead className="text-right">Lots</TableHead>
          <TableHead className="text-right">Price</TableHead>
          <TableHead className="w-24">Status</TableHead>
          <TableHead>Rationale</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {ordered.map((r, i) => (
          <TableRow key={`${r.timestamp}-${r.tradingsymbol}-${i}`}>
            <TableCell className="font-mono text-xs">{formatTime(r.timestamp)}</TableCell>
            <TableCell>
              <Badge variant={r.kind === "ENTRY" ? "default" : "secondary"} className="text-[10px]">
                {r.kind}
              </Badge>
            </TableCell>
            <TableCell>
              <Badge
                variant={r.transaction_type === "BUY" ? "success" : "destructive"}
                className="text-[10px]"
              >
                {r.transaction_type}
              </Badge>
            </TableCell>
            <TableCell className="font-mono text-xs">{r.tradingsymbol}</TableCell>
            <TableCell className="text-right tabular-nums">{r.quantity}</TableCell>
            <TableCell className="text-right tabular-nums">{formatNum(r.price)}</TableCell>
            <TableCell>
              <Badge variant="outline" className="text-[10px]">
                {r.status}
              </Badge>
            </TableCell>
            <TableCell className="max-w-md truncate text-xs text-muted-foreground" title={r.rationale}>
              {r.rationale}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}


// ──────────────────────────────────────────────────────────
// Pair-trading grouped view
// ──────────────────────────────────────────────────────────
//
// PairTradingStrategy emits the two legs of an entry/exit batch in
// immediate succession with the same `rationale` string (built once and
// shared in `_build_entry_proposals` / `_build_exit_proposals`). We group
// adjacent rows by `rationale` so each batch renders as a single card
// with the long and short legs explicitly labelled — answering "buy what,
// sell what" at a glance instead of forcing the operator to mentally
// pair two independent-looking table rows.

type PairBatch = {
  rationale: string;
  kind: "ENTRY" | "REHEDGE";
  timestamp: string;
  // Every leg in proposal order. We deliberately don't bucket into BUY/SELL
  // slots: pairs with NEGATIVE hedge ratios (e.g. COALINDIA/ITC, β = -0.54)
  // produce two same-side legs by design (LONG_SPREAD = BUY both,
  // SHORT_SPREAD = SELL both), and a fixed BUY/SELL grid would silently
  // drop the second same-side leg.
  legs: ProposalEntry[];
};

function groupPairBatches(rows: ProposalEntry[]): PairBatch[] {
  const batches: PairBatch[] = [];
  let cur: PairBatch | null = null;
  for (const r of rows) {
    if (cur === null || cur.rationale !== r.rationale) {
      cur = {
        rationale: r.rationale,
        kind: r.kind,
        timestamp: r.timestamp,
        legs: [],
      };
      batches.push(cur);
    }
    cur.legs.push(r);
  }
  return batches;
}

// True iff every leg in the batch carries the same transaction_type.
// Used to surface a hint about negative-β pair convention to operators
// who reasonably expect "buy one, sell the other" but see two of the
// same side.
function legsSameSide(legs: ProposalEntry[]): boolean {
  if (legs.length < 2) return false;
  const first = legs[0].transaction_type;
  return legs.every((l) => l.transaction_type === first);
}

// Best-effort title from the rationale string. Pair rationales are shaped
// like "LONG_SPREAD on AAA/BBB z=-2.34 …" or "EXIT_MEAN_REVERT on AAA/BBB
// z=-0.4 …" — we extract the action word and the pair so the card has a
// clean header, while the full rationale stays available below.
function summarizeRationale(rationale: string): { action: string; pair: string | null } {
  const m = rationale.match(/^(\S+)\s+on\s+(\S+)/);
  if (!m) return { action: rationale.split(/\s+/)[0] ?? "ACTION", pair: null };
  return { action: m[1], pair: m[2] };
}

function actionBadgeVariant(action: string): "default" | "secondary" | "destructive" | "outline" {
  if (action.startsWith("EXIT_") || action.startsWith("EXIT")) return "secondary";
  if (action.includes("STOP")) return "destructive";
  return "default";
}

function PairTradeGroupedView({ rows }: { rows: ProposalEntry[] }) {
  const batches = groupPairBatches(rows);
  // Newest first, mirroring the table view.
  const ordered = [...batches].reverse();

  return (
    <div className="space-y-3">
      {ordered.map((b, i) => {
        const { action, pair } = summarizeRationale(b.rationale);
        const sameSide = legsSameSide(b.legs);
        return (
          <div
            key={`${b.timestamp}-${i}`}
            className="rounded-md border border-border bg-card/40 p-3"
          >
            <div className="flex flex-wrap items-center gap-2 pb-2">
              <Badge variant={b.kind === "ENTRY" ? "default" : "secondary"} className="text-[10px]">
                {b.kind}
              </Badge>
              <Badge variant={actionBadgeVariant(action)} className="text-[10px]">
                {action}
              </Badge>
              {pair && (
                <span className="font-mono text-xs text-muted-foreground">{pair}</span>
              )}
              <span className="ml-auto font-mono text-xs text-muted-foreground">
                {formatTime(b.timestamp)}
              </span>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              {b.legs.length === 0 ? (
                <div className="rounded-md border border-dashed border-border/60 px-3 py-2 text-xs text-muted-foreground">
                  no legs recorded
                </div>
              ) : (
                b.legs.map((leg, j) => <PairLegRow key={j} leg={leg} />)
              )}
            </div>

            {sameSide && (
              <div className="mt-2 rounded-sm bg-muted/30 px-2 py-1 text-[11px] text-muted-foreground">
                Both legs are <span className="font-semibold">{b.legs[0].transaction_type}</span> — this pair has a
                <span className="font-mono"> negative hedge ratio</span>, so the cointegrating
                combination requires same-side legs (e.g. <em>LONG_SPREAD</em> = BUY both,
                <em> SHORT_SPREAD</em> = SELL both).
              </div>
            )}

            <div className="pt-2 text-[11px] text-muted-foreground" title={b.rationale}>
              {b.rationale}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function PairLegRow({ leg }: { leg: ProposalEntry }) {
  const isBuy = leg.transaction_type === "BUY";
  return (
    <div
      className={
        "flex items-center gap-2 rounded-md border px-3 py-2 " +
        (isBuy
          ? "border-success/40 bg-success/5"
          : "border-destructive/40 bg-destructive/5")
      }
    >
      {isBuy ? (
        <ArrowUpRight className="h-4 w-4 text-success" />
      ) : (
        <ArrowDownRight className="h-4 w-4 text-destructive" />
      )}
      <Badge
        variant={isBuy ? "success" : "destructive"}
        className="text-[10px] font-semibold tracking-wide"
      >
        {leg.transaction_type}
      </Badge>
      <span className="font-mono text-xs">{leg.tradingsymbol}</span>
      <span className="ml-auto flex items-center gap-2 text-xs tabular-nums">
        <span className="text-muted-foreground">{leg.quantity} lot</span>
        <span className="font-medium">@ ₹{formatNum(leg.price)}</span>
      </span>
    </div>
  );
}
