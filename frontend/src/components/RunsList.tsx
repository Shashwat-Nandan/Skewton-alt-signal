import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import type { RunStatus } from "@/lib/types";
import { shortId, formatTime } from "@/lib/utils";

const STATUS_VARIANT: Record<RunStatus, "default" | "secondary" | "destructive" | "success" | "outline"> = {
  RUNNING: "success",
  STOPPING: "secondary",
  STOPPED: "outline",
  ERRORED: "destructive",
};

const RUN_LIST_LIMIT = 10;

export function RunsList() {
  const { data: runs, isLoading } = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 3000,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Runs</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
          </div>
        ) : !runs || runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">No runs yet.</p>
        ) : (
          <ul className="divide-y divide-border">
            {[...runs]
              .sort((a, b) => b.created_at.localeCompare(a.created_at))
              .slice(0, RUN_LIST_LIMIT)
              .map((r) => (
                <li key={r.id}>
                  <Link
                    to={`/runs/${r.id}`}
                    className="flex items-center justify-between gap-3 py-3 transition-colors hover:bg-accent/40 -mx-2 px-2 rounded-md"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{r.strategy_name}</span>
                        <Badge variant="outline" className="text-[10px] uppercase">
                          {r.mode}
                        </Badge>
                        <Badge variant={STATUS_VARIANT[r.status]} className="text-[10px]">
                          {r.status}
                        </Badge>
                      </div>
                      <div className="mt-0.5 text-xs text-muted-foreground">
                        <span className="font-mono">{shortId(r.id)}</span>
                        {" · "}
                        ticks {r.tick_count}
                        {" · "}
                        last {formatTime(r.last_tick_at)}
                      </div>
                    </div>
                    <ArrowRight className="h-4 w-4 text-muted-foreground" />
                  </Link>
                </li>
              ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
