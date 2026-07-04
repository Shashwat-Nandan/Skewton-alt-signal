import { useQuery } from "@tanstack/react-query";
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

const POLL_MS = 60_000;

function pnlClass(v: number | null | undefined): string {
  return cn(
    "tabular-nums",
    (v ?? 0) > 0 && "text-emerald-600",
    (v ?? 0) < 0 && "text-rose-600",
  );
}

function PositionBadge({ position }: { position: string }) {
  if (position === "LONG_SPREAD")
    return <Badge className="bg-emerald-600/15 text-emerald-700 hover:bg-emerald-600/15">LONG</Badge>;
  if (position === "SHORT_SPREAD")
    return <Badge className="bg-rose-600/15 text-rose-700 hover:bg-rose-600/15">SHORT</Badge>;
  return <Badge variant="outline" className="text-muted-foreground">flat</Badge>;
}

/** ADF regime gate state: only enter when the raw cointegration residual is
 *  currently stationary. Precedence: unknown → gate-off → STALE → OPEN →
 *  blocked. "gate off" is inferred from gate_open=true with no p-value: a live
 *  open gate always has a computed p, so open-without-p means adf_gate_p<=0 (the
 *  gate is disabled, not passing). STALE (window predates a data gap →
 *  fail-closed) only applies when the gate is actually on. */
function RegimeBadge({
  open,
  stale,
  p,
}: {
  open: boolean | null;
  stale: boolean | null;
  p: number | null;
}) {
  if (open == null && stale == null)
    return <span className="text-muted-foreground">—</span>;
  // p shown at 4dp to match the candidates page and read cleanly near the ~0.05 gate.
  const pStr = p == null ? null : `p=${formatNum(p, 4)}`;
  let badge;
  if (open === true && p == null) {
    // gate disabled (adf_gate_p<=0): entries permitted, no ADF evaluation.
    badge = <Badge variant="outline" className="text-muted-foreground" title="ADF regime gate disabled (adf_gate_p ≤ 0)">gate off</Badge>;
  } else if (stale === true) {
    badge = <Badge className="bg-amber-600/15 text-amber-700 hover:bg-amber-600/15" title="Residual window predates a data gap — gate fail-closed (issue #65)">STALE</Badge>;
  } else if (open === true) {
    badge = <Badge className="bg-emerald-600/15 text-emerald-700 hover:bg-emerald-600/15" title="ADF gate open — raw residual is stationary, new entries permitted">OPEN</Badge>;
  } else if (open === false) {
    badge = <Badge variant="outline" className="text-muted-foreground" title="ADF gate blocked — residual non-stationary (p above threshold) or window unassessable">blocked</Badge>;
  } else {
    // gate_open unknown (partial/older sidecar) — do not imply 'blocked'.
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      {badge}
      {pStr && <span className="text-xs tabular-nums text-muted-foreground">{pStr}</span>}
    </span>
  );
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
      </CardContent>
    </Card>
  );
}

export function KalmanPairsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["kalman-pairs"],
    queryFn: () => api.kalmanPairs(),
    refetchInterval: POLL_MS,
  });

  if (isLoading) return <Skeleton className="h-96" />;
  if (isError)
    return (
      <Card>
        <CardContent className="pt-6 text-sm text-rose-600">
          Failed to load Kalman pair data.
        </CardContent>
      </Card>
    );

  const pairs = data?.pairs ?? [];
  const hasData = (data?.latest_date ?? null) !== null;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Kalman pairs — monitoring</h1>
        <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          <Info className="h-3.5 w-3.5" />
          Time-varying hedge ratio γ_t (paper, forward A/B vs the static book). Snapshot of the
          latest completed session{data?.latest_date ? ` (${data.latest_date})` : ""}; the
          sidecar is written once per session at 15:25 IST.
        </p>
      </div>

      {!hasData ? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No Kalman session has produced a sidecar yet. Once the
            <code className="mx-1 rounded bg-muted px-1">kalman-pairs-paper</code>
            runner completes its first session, the monitored pairs — each with its tracked γ,
            current z-score, position, and risk band — appear here.
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <MetricCard label="Pairs monitored" value={String(data?.n_pairs ?? 0)} />
            <MetricCard label="Open positions" value={String(pairs.filter((p) => p.position !== "FLAT").length)} />
            <MetricCard label="Session P&L" value={formatINR(data?.session_pnl ?? 0)} />
          </div>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                Monitored pairs <span className="text-xs font-normal text-muted-foreground">(open / nearest-to-signal first)</span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Pair</TableHead>
                    <TableHead>Position</TableHead>
                    <TableHead className="text-right">γ (filter)</TableHead>
                    <TableHead className="text-right">μ</TableHead>
                    <TableHead className="text-right">z</TableHead>
                    <TableHead className="text-right">entry z</TableHead>
                    <TableHead>regime gate</TableHead>
                    <TableHead className="text-right">stop ₹</TableHead>
                    <TableHead className="text-right">target ₹</TableHead>
                    <TableHead className="text-right">session P&L</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pairs.map((p, i) => (
                    <TableRow key={`${p.pair}-${p.model ?? ""}-${i}`}>
                      <TableCell className="font-medium">{p.pair}</TableCell>
                      <TableCell><PositionBadge position={p.position} /></TableCell>
                      <TableCell className="text-right tabular-nums">{formatNum(p.gamma, 4)}</TableCell>
                      <TableCell className="text-right tabular-nums">{formatNum(p.mu, 4)}</TableCell>
                      <TableCell
                        className={cn(
                          "text-right tabular-nums",
                          Math.abs(p.current_z ?? 0) >= 2 && "font-semibold",
                        )}
                      >
                        {formatNum(p.current_z, 2)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {p.position === "FLAT" ? "—" : formatNum(p.entry_z, 2)}
                      </TableCell>
                      <TableCell>
                        <RegimeBadge
                          open={p.regime_gate_open}
                          stale={p.regime_stale}
                          p={p.regime_adf_p}
                        />
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {p.stop_inr == null ? "—" : formatINR(p.stop_inr)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {p.target_inr == null ? "—" : formatINR(p.target_inr)}
                      </TableCell>
                      <TableCell className={cn("text-right", pnlClass(p.day_pnl))}>
                        {formatINR(p.day_pnl)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
