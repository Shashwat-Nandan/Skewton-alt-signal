import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { MarketProfileChart } from "@/components/MarketProfileChart";
import type { DayProfile } from "@/lib/types";

const DEFAULT_DAYS = 60;

export function MarketProfilePage() {
  const symbolsQ = useQuery({
    queryKey: ["mp", "symbols"],
    queryFn: api.marketProfileSymbols,
  });

  const [symbol, setSymbol] = useState<string | null>(null);
  const [days, setDays] = useState(DEFAULT_DAYS);
  const [mode, setMode] = useState<"composite" | "daily">("composite");

  // Default to the first symbol once we have data.
  const effectiveSymbol = useMemo(() => {
    if (symbol) return symbol;
    return symbolsQ.data?.[0]?.symbol ?? null;
  }, [symbol, symbolsQ.data]);

  const profileQ = useQuery({
    queryKey: ["mp", "profile", effectiveSymbol, days, mode],
    queryFn: () =>
      api.marketProfile(effectiveSymbol!, { days, mode, period_minutes: 30 }),
    enabled: !!effectiveSymbol,
    staleTime: 60_000,
  });

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Market Profile</CardTitle>
          <CardDescription>
            30-minute TPO profile per stock. Pick a symbol, a lookback window,
            and a render mode. Composite rolls every bar in the window into one
            profile; Daily renders each trading day separately with TPO letters.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-4">
          <SymbolPicker
            symbols={symbolsQ.data ?? []}
            value={effectiveSymbol}
            onChange={setSymbol}
            loading={symbolsQ.isLoading}
          />
          <div className="space-y-1">
            <Label htmlFor="days">Lookback (days)</Label>
            <Input
              id="days"
              type="number"
              min={1}
              max={720}
              value={days}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isFinite(n) && n > 0) setDays(n);
              }}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="mode">Mode</Label>
            <Select value={mode} onValueChange={(v) => setMode(v as typeof mode)}>
              <SelectTrigger id="mode">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="composite">Composite</SelectItem>
                <SelectItem value="daily">Daily</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <CoverageHint
            row={
              symbolsQ.data?.find((r) => r.symbol === effectiveSymbol) ?? null
            }
          />
        </CardContent>
      </Card>

      {symbolsQ.isLoading && <Skeleton className="h-96" />}

      {!symbolsQ.isLoading && (symbolsQ.data?.length ?? 0) === 0 && (
        <EmptyUniverse />
      )}

      {effectiveSymbol && profileQ.isLoading && (
        <Skeleton className="h-[640px]" />
      )}

      {profileQ.isError && (
        <Card>
          <CardContent className="pt-6 text-sm text-destructive">
            {(profileQ.error as Error).message}
          </CardContent>
        </Card>
      )}

      {profileQ.data && profileQ.data.composite && mode === "composite" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Composite — last {profileQ.data.lookback_days} days,{" "}
              {profileQ.data.n_bars} bars over{" "}
              {profileQ.data.composite.n_days} trading days
            </CardTitle>
            <ProfileLevels p={profileQ.data.composite} />
          </CardHeader>
          <CardContent>
            <MarketProfileChart
              profile={profileQ.data.composite}
              height={620}
              showLetters={false}
            />
          </CardContent>
        </Card>
      )}

      {profileQ.data && mode === "daily" && (profileQ.data.daily ?? []).length > 0 && (
        <DailyGrid days={profileQ.data.daily!} />
      )}
    </div>
  );
}

function SymbolPicker({
  symbols,
  value,
  onChange,
  loading,
}: {
  symbols: { symbol: string; name?: string | null }[];
  value: string | null;
  onChange: (s: string) => void;
  loading: boolean;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor="symbol">Symbol</Label>
      <Select
        value={value ?? undefined}
        onValueChange={onChange}
        disabled={loading || symbols.length === 0}
      >
        <SelectTrigger id="symbol">
          <SelectValue placeholder={loading ? "Loading…" : "Pick a symbol"} />
        </SelectTrigger>
        <SelectContent>
          {symbols.map((s) => (
            <SelectItem key={s.symbol} value={s.symbol}>
              <span className="font-medium">{s.symbol}</span>
              {s.name && (
                <span className="ml-2 text-xs text-muted-foreground">
                  {s.name}
                </span>
              )}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function CoverageHint({
  row,
}: {
  row: {
    earliest_bar_ts?: string | null;
    latest_bar_ts?: string | null;
    last_update_at?: string | null;
  } | null;
}) {
  if (!row) return null;
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      <Label className="text-foreground">Coverage</Label>
      <div>
        {row.earliest_bar_ts ? row.earliest_bar_ts.slice(0, 10) : "—"} →{" "}
        {row.latest_bar_ts ? row.latest_bar_ts.slice(0, 10) : "—"}
      </div>
      {row.last_update_at && (
        <div>Last update: {row.last_update_at.slice(0, 16).replace("T", " ")}</div>
      )}
    </div>
  );
}

function ProfileLevels({
  p,
}: {
  p: { poc: number; vah: number; val: number; high: number; low: number; total_tpos: number };
}) {
  return (
    <div className="flex flex-wrap gap-2 pt-2">
      <Badge variant="default">POC {p.poc.toFixed(2)}</Badge>
      <Badge variant="secondary">VAH {p.vah.toFixed(2)}</Badge>
      <Badge variant="secondary">VAL {p.val.toFixed(2)}</Badge>
      <Badge variant="outline">High {p.high.toFixed(2)}</Badge>
      <Badge variant="outline">Low {p.low.toFixed(2)}</Badge>
      <Badge variant="outline">Total TPOs {p.total_tpos}</Badge>
    </div>
  );
}

function DailyGrid({ days }: { days: DayProfile[] }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {days.slice(-12).reverse().map((d) => (
        <Card key={d.day}>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">{d.day}</CardTitle>
            <div className="flex flex-wrap gap-1.5 pt-1">
              <Badge variant="default" className="text-[10px]">
                POC {d.poc.toFixed(2)}
              </Badge>
              <Badge variant="secondary" className="text-[10px]">
                VAH {d.vah.toFixed(2)}
              </Badge>
              <Badge variant="secondary" className="text-[10px]">
                VAL {d.val.toFixed(2)}
              </Badge>
              {d.ib_high != null && d.ib_low != null && (
                <Badge variant="outline" className="text-[10px]">
                  IB {d.ib_low.toFixed(2)}–{d.ib_high.toFixed(2)}
                </Badge>
              )}
              <Badge variant="outline" className="text-[10px]">
                {d.n_periods}p · O {d.open.toFixed(2)} / C {d.close.toFixed(2)}
              </Badge>
            </div>
          </CardHeader>
          <CardContent>
            <MarketProfileChart profile={d} height={320} />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function EmptyUniverse() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>No symbols ingested yet</CardTitle>
        <CardDescription>
          Run the ingestion script to populate the bars table:
        </CardDescription>
      </CardHeader>
      <CardContent>
        <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">
{`python -m market_data.fetch_bars --backfill --days 90
# or pick a few symbols:
python -m market_data.fetch_bars --backfill --days 90 --symbols RELIANCE,INFY,HDFCBANK
# then daily:
python -m market_data.fetch_bars --update`}
        </pre>
      </CardContent>
    </Card>
  );
}
