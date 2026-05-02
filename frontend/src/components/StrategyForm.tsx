import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Play, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import type { ExecutionMode } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ParamForm, coerceParams } from "./ParamForm";

const MODES: { value: ExecutionMode; label: string; hint: string }[] = [
  { value: "signals", label: "Signals only", hint: "Log proposals to JSONL feed; do not place anything." },
  { value: "paper", label: "Paper", hint: "Simulate fills against real market data; no real orders." },
];

export function StrategyForm() {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const { data: strategies, isLoading } = useQuery({
    queryKey: ["strategies"],
    queryFn: api.listStrategies,
  });

  const [strategyName, setStrategyName] = useState<string>("");
  const [mode, setMode] = useState<ExecutionMode>("signals");
  const [paramValues, setParamValues] = useState<Record<string, string>>({});

  // Default to first strategy once loaded
  const selected = useMemo(() => {
    if (!strategies || strategies.length === 0) return undefined;
    return strategies.find((s) => s.name === strategyName) ?? strategies[0];
  }, [strategies, strategyName]);

  // Reset param values whenever the strategy changes
  function selectStrategy(name: string) {
    setStrategyName(name);
    setParamValues({});
  }

  const createRun = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error("No strategy selected");
      return api.createRun({
        strategy: selected.name,
        mode,
        params: coerceParams(selected.params, paramValues),
      });
    },
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/runs/${run.id}`);
    },
  });

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>New Run</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New Run</CardTitle>
        <CardDescription>
          Pick a strategy, choose how it should execute, override any params, then start.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="space-y-1.5">
          <Label htmlFor="strategy">Strategy</Label>
          <Select value={selected?.name ?? ""} onValueChange={selectStrategy}>
            <SelectTrigger id="strategy">
              <SelectValue placeholder="Select a strategy" />
            </SelectTrigger>
            <SelectContent>
              {strategies?.map((s) => (
                <SelectItem key={s.name} value={s.name}>
                  <div className="flex flex-col">
                    <span className="font-medium">{s.name}</span>
                    <span className="text-xs text-muted-foreground">{s.description}</span>
                  </div>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label>Mode</Label>
          <div className="grid grid-cols-2 gap-2">
            {MODES.map((m) => (
              <button
                key={m.value}
                type="button"
                onClick={() => setMode(m.value)}
                className={`rounded-md border p-3 text-left text-sm transition-colors ${
                  mode === m.value
                    ? "border-primary bg-primary/10"
                    : "border-input hover:bg-accent"
                }`}
              >
                <div className="font-medium">{m.label}</div>
                <div className="text-xs text-muted-foreground">{m.hint}</div>
              </button>
            ))}
          </div>
          <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
            <AlertCircle className="mt-0.5 h-3 w-3 flex-shrink-0" />
            Live trading is disabled in this build. Use the headless daemon path for live.
          </p>
        </div>

        <div className="space-y-2">
          <Label>Parameters</Label>
          {selected && (
            <ParamForm
              params={selected.params}
              values={paramValues}
              onChange={setParamValues}
            />
          )}
        </div>

        <Button
          className="w-full"
          size="lg"
          onClick={() => createRun.mutate()}
          disabled={!selected || createRun.isPending}
        >
          <Play className="mr-2 h-4 w-4" />
          {createRun.isPending ? "Starting…" : "Start Run"}
        </Button>

        {createRun.isError && (
          <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {createRun.error.message}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
