import { Card, CardContent } from "@/components/ui/card";
import { cn, formatINR, formatNum } from "@/lib/utils";

type Tile = {
  label: string;
  value: string;
  hint?: string;
  /** Visual emphasis based on sign */
  signed?: number | null;
};

function MetricTile({ label, value, hint, signed }: Tile) {
  const tone =
    signed == null
      ? ""
      : signed > 0
      ? "text-success"
      : signed < 0
      ? "text-destructive"
      : "";
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className={cn("mt-1 text-2xl font-semibold tabular-nums", tone)}>{value}</div>
        {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  );
}

type Props = {
  report: Record<string, unknown> | null | undefined;
  tickCount: number;
  nSignals: number;
  nTrades: number;
};

export function MetricsRow({ report, tickCount, nSignals, nTrades }: Props) {
  const realized = num(report?.realized_pnl);
  const unrealized = num(report?.unrealized_pnl);
  const total = realized != null && unrealized != null ? realized + unrealized : null;
  const costs = num(report?.transaction_costs ?? report?.total_transaction_costs);
  const currentZ = num(report?.current_z);

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      <MetricTile
        label="Net P&L"
        value={total != null ? formatINR(total) : "—"}
        signed={total}
      />
      <MetricTile
        label="Realized"
        value={realized != null ? formatINR(realized) : "—"}
        signed={realized}
      />
      <MetricTile
        label="Unrealized"
        value={unrealized != null ? formatINR(unrealized) : "—"}
        signed={unrealized}
      />
      <MetricTile
        label="Costs"
        value={costs != null ? formatINR(costs) : "—"}
      />
      <MetricTile
        label={currentZ != null ? "Z-score" : "Ticks"}
        value={currentZ != null ? formatNum(currentZ) : String(tickCount)}
        hint={currentZ != null ? "Spread vs rolling mean" : undefined}
      />
      <MetricTile
        label="Activity"
        value={`${nSignals + nTrades}`}
        hint={`${nSignals} signals · ${nTrades} fills`}
      />
    </div>
  );
}

function num(v: unknown): number | null {
  if (typeof v === "number" && !Number.isNaN(v)) return v;
  return null;
}
