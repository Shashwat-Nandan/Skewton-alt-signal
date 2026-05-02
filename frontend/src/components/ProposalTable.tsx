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
import { formatTime, formatNum } from "@/lib/utils";

type Props = {
  rows: ProposalEntry[];
  emptyMessage?: string;
};

export function ProposalTable({ rows, emptyMessage = "No entries yet." }: Props) {
  if (rows.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">{emptyMessage}</p>;
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
