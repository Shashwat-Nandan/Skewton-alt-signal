import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * Dashboard password gate. This is the FIRST thing a visitor sees — even
 * before the broker login (Kotak Neo by default). The session cookie issued on success is
 * what every subsequent request rides on; mid-session expiry returns
 * the user here via the global 401 handler in main.tsx.
 */
export function DashboardLoginPage() {
  const qc = useQueryClient();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const login = useMutation({
    mutationFn: () => api.sessionLogin(password),
    onSuccess: () => {
      setError(null);
      setPassword("");
      // Invalidate the session query so the gate in App.tsx re-renders
      // and shows the actual app instead of this page.
      qc.invalidateQueries({ queryKey: ["session"] });
    },
    onError: (e: Error) => {
      setError(e.message.startsWith("401") ? "Incorrect password" : e.message);
    },
  });

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!password) return;
    login.mutate();
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-2xl">
            <Lock className="h-5 w-5" />
            Dashboard
          </CardTitle>
          <CardDescription>Enter the dashboard password to continue.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="dashboard-password">Password</Label>
              <Input
                id="dashboard-password"
                type="password"
                autoComplete="current-password"
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={login.isPending}
                aria-invalid={!!error}
                aria-describedby={error ? "dashboard-password-error" : undefined}
              />
            </div>
            {error && (
              <div
                id="dashboard-password-error"
                className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
              >
                {error}
              </div>
            )}
            <Button
              type="submit"
              size="lg"
              className="w-full"
              disabled={login.isPending || !password}
            >
              {login.isPending ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
