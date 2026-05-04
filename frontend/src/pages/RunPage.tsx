import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Square, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import type { RunStatus } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricsRow } from "@/components/MetricsRow";
import { ProposalTable } from "@/components/ProposalTable";
import { PnLChart } from "@/components/PnLChart";
import { EODReportCard } from "@/components/EODReportCard";
import { shortId, formatTime } from "@/lib/utils";

const STATUS_VARIANT: Record<RunStatus, "default" | "secondary" | "destructive" | "success" | "outline"> = {
  RUNNING: "success",
  STOPPING: "secondary",
  STOPPED: "outline",
  ERRORED: "destructive",
};

export function RunPage() {
  const { runId = "" } = useParams<{ runId: string }>();
  const qc = useQueryClient();

  const { data: run, isLoading, error } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.getRun(runId),
    refetchInterval: (q) => {
      const r = q.state.data;
      // Poll fast while running, slow once done.
      return r && (r.status === "RUNNING" || r.status === "STOPPING") ? 2000 : false;
    },
    enabled: Boolean(runId),
  });

  const stop = useMutation({
    mutationFn: () => api.stopRun(runId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", runId] }),
  });

  if (isLoading) return <Skeleton className="h-screen" />;
  if (error || !run) {
    return (
      <Card>
        <CardContent className="py-12 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-destructive" />
          <p className="mt-2 text-sm">Run not found.</p>
          <Button asChild variant="link" className="mt-4">
            <Link to="/"><ArrowLeft className="mr-2 h-4 w-4" />Back to dashboard</Link>
          </Button>
        </CardContent>
      </Card>
    );
  }

  const isRunning = run.status === "RUNNING";
  const isSignalsMode = run.mode === "signals";
  const primaryRows = isSignalsMode ? run.signals : run.trades;
  const primaryLabel = isSignalsMode ? "Signals" : "Trades";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-1">
          <Button asChild variant="link" size="sm" className="-ml-3 h-auto p-0 text-muted-foreground">
            <Link to="/"><ArrowLeft className="mr-1 h-3 w-3" />All runs</Link>
          </Button>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-lg font-semibold sm:text-xl">{run.strategy_name}</h1>
            <Badge variant="outline" className="uppercase">{run.mode}</Badge>
            <Badge variant={STATUS_VARIANT[run.status]}>{run.status}</Badge>
          </div>
          <div className="text-xs text-muted-foreground">
            <span className="font-mono">{shortId(run.id, 8)}</span>
            {" · "}
            ticks {run.tick_count}
            {" · "}
            last {formatTime(run.last_tick_at)}
          </div>
        </div>
        {isRunning && (
          <Button
            variant="destructive"
            onClick={() => stop.mutate()}
            disabled={stop.isPending}
            className="shrink-0"
          >
            <Square className="mr-2 h-4 w-4" />
            {stop.isPending ? "Stopping…" : "Stop run"}
          </Button>
        )}
      </div>

      {run.error && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="py-4 text-sm text-destructive">
            <AlertTriangle className="mr-2 inline h-4 w-4" />
            {run.error}
          </CardContent>
        </Card>
      )}

      <MetricsRow
        report={run.last_eod_report}
        tickCount={run.tick_count}
        nSignals={run.n_signals}
        nTrades={run.n_trades}
      />

      <PnLChart history={run.pnl_history} />

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Activity</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="primary">
            <TabsList>
              <TabsTrigger value="primary">{primaryLabel}</TabsTrigger>
              <TabsTrigger value="report">Snapshot</TabsTrigger>
            </TabsList>
            <TabsContent value="primary">
              <ProposalTable
                rows={primaryRows}
                strategyName={run.strategy_name}
                emptyMessage={
                  isSignalsMode
                    ? "No signals emitted yet."
                    : "No fills yet — strategy is waiting for entry conditions."
                }
              />
            </TabsContent>
            <TabsContent value="report">
              <EODReportCard report={run.last_eod_report} />
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  );
}
