import { useQuery } from "@tanstack/react-query";
import { Info, ShieldAlert, ShieldCheck } from "lucide-react";
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
import { cn, formatINR } from "@/lib/utils";

const POLL_MS = 60_000;

function pnlClass(v: number | null | undefined): string {
  return cn(
    "tabular-nums",
    (v ?? 0) > 0 && "text-emerald-600",
    (v ?? 0) < 0 && "text-rose-600",
  );
}

function PosBadge({ pos }: { pos: number }) {
  if (pos > 0)
    return <Badge className="bg-emerald-600/15 text-emerald-700 hover:bg-emerald-600/15">long</Badge>;
  if (pos < 0)
    return <Badge className="bg-rose-600/15 text-rose-700 hover:bg-rose-600/15">short</Badge>;
  return <Badge variant="outline" className="text-muted-foreground">flat</Badge>;
}

/** The checker verdict is a string: 'pass' | 'REJECT: …' | 'skipped:…' | 'deferred…'. */
function CheckerBadge({ verdict }: { verdict: string | null }) {
  if (!verdict) return <Badge variant="outline" className="text-muted-foreground">—</Badge>;
  if (verdict === "pass")
    return <Badge className="bg-emerald-600/15 text-emerald-700 hover:bg-emerald-600/15">pass</Badge>;
  if (verdict.startsWith("REJECT"))
    return <Badge className="bg-rose-600/15 text-rose-700 hover:bg-rose-600/15">REJECT</Badge>;
  // skipped / deferred / ERROR — a non-verdict
  return <Badge variant="outline" className="text-muted-foreground">{verdict.split(":")[0]}</Badge>;
}

function MetricCard({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className={cn("mt-1 text-xl font-semibold tabular-nums", valueClass)}>{value}</div>
      </CardContent>
    </Card>
  );
}

export function KalmanTrendPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["kalman-trend"],
    queryFn: () => api.kalmanTrend(),
    refetchInterval: POLL_MS,
  });

  if (isLoading) return <Skeleton className="h-96" />;
  if (isError)
    return (
      <Card>
        <CardContent className="pt-6 text-sm text-rose-600">
          Failed to load Kalman-trend loop data.
        </CardContent>
      </Card>
    );

  const instruments = data?.instruments ?? [];
  const hasData = (data?.latest_date ?? null) !== null;
  const loop = data?.loop ?? null;
  const halted = loop?.risk === "HALT_NEW_ENTRIES";

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Kalman trend — loop engine</h1>
        <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          <Info className="h-3.5 w-3.5" />
          Self-improving loop pilot (paper-only). Kalman-vs-MA intraday A/B with an independent
          checker + isolated kill switch. The strategy is NO-GO vs MA — this surfaces the loop's
          honest verdict, it does not assert an edge.
          {data?.latest_date ? ` Latest session ${data.latest_date}.` : ""}
        </p>
      </div>

      {!hasData ? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No Kalman-trend session has produced a sidecar yet. Once the
            <code className="mx-1 rounded bg-muted px-1">loop-kalman-trend</code>
            orchestrator completes its first session, the per-instrument Kalman-vs-MA performance,
            positions, the checker verdict, the kill-switch status, and the compounding lessons
            appear here.
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <MetricCard label="Kalman ₹ (session)" value={formatINR(data?.total_kalman_rupees ?? 0)} valueClass={pnlClass(data?.total_kalman_rupees)} />
            <MetricCard label="MA ₹ (session)" value={formatINR(data?.total_ma_rupees ?? 0)} valueClass={pnlClass(data?.total_ma_rupees)} />
            <MetricCard label="Kalman − MA" value={formatINR(data?.kalman_minus_ma_rupees ?? 0)} valueClass={pnlClass(data?.kalman_minus_ma_rupees)} />
            <MetricCard label="Sessions recorded" value={String(data?.n_sessions_recorded ?? 0)} />
          </div>

          {/* Loop-engineering status: the bits the other strategy tabs don't have. */}
          <Card className={cn(halted && "border-rose-300")}>
            <CardHeader className="pb-2">
              <CardTitle className="text-base flex items-center gap-2">
                {halted ? <ShieldAlert className="h-4 w-4 text-rose-600" /> : <ShieldCheck className="h-4 w-4 text-emerald-600" />}
                Loop status
              </CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-4 text-sm">
              <div>
                <div className="text-xs uppercase tracking-wide text-muted-foreground">Checker verdict</div>
                <div className="mt-1"><CheckerBadge verdict={loop?.checker ?? null} /></div>
                {loop?.checker?.startsWith("REJECT") && (
                  <div className="mt-1 text-xs text-muted-foreground">{loop.checker.replace(/^REJECT:\s*/, "")}</div>
                )}
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide text-muted-foreground">Kill switch</div>
                <div className="mt-1">
                  {halted
                    ? <Badge className="bg-rose-600/15 text-rose-700 hover:bg-rose-600/15">HALT_NEW_ENTRIES</Badge>
                    : <Badge className="bg-emerald-600/15 text-emerald-700 hover:bg-emerald-600/15">ok</Badge>}
                </div>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide text-muted-foreground">Status</div>
                <div className="mt-1 font-medium">{loop?.status ?? "—"}</div>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide text-muted-foreground">Last run</div>
                <div className="mt-1 tabular-nums text-muted-foreground">{loop?.timestamp ?? "—"}</div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                Instruments <span className="text-xs font-normal text-muted-foreground">(Kalman vs MA, session end)</span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Kalman pos</TableHead>
                    <TableHead className="text-right">Kalman ₹</TableHead>
                    <TableHead className="text-right">Kalman trades</TableHead>
                    <TableHead>MA pos</TableHead>
                    <TableHead className="text-right">MA ₹</TableHead>
                    <TableHead className="text-right">MA trades</TableHead>
                    <TableHead className="text-right">edge (K−MA)</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {instruments.map((i) => (
                    <TableRow key={i.symbol}>
                      <TableCell className="font-medium">{i.symbol}</TableCell>
                      <TableCell><PosBadge pos={i.kalman.open_pos} /></TableCell>
                      <TableCell className={cn("text-right", pnlClass(i.kalman.realized_rupees))}>{formatINR(i.kalman.realized_rupees)}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">{i.kalman.n_trades}</TableCell>
                      <TableCell><PosBadge pos={i.ma.open_pos} /></TableCell>
                      <TableCell className={cn("text-right", pnlClass(i.ma.realized_rupees))}>{formatINR(i.ma.realized_rupees)}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">{i.ma.n_trades}</TableCell>
                      <TableCell className={cn("text-right font-medium", pnlClass(i.edge_rupees))}>{formatINR(i.edge_rupees)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          {/* Compounding lessons — the loop's self-improvement memory (newest first). */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">
                Lessons <span className="text-xs font-normal text-muted-foreground">(loop memory, newest first)</span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              {(data?.lessons ?? []).length === 0 ? (
                <div className="text-sm text-muted-foreground">No lessons recorded yet.</div>
              ) : (
                <ul className="space-y-1.5 text-sm">
                  {data?.lessons.map((le, idx) => (
                    <li key={idx} className="flex gap-2">
                      <span className="text-muted-foreground">•</span>
                      <span className={cn(le.includes("RISK KILL") && "text-rose-600")}>{le}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
