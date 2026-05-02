import { useState } from "react";
import { LogIn } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function LoginCard() {
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleLogin() {
    setLoading(true);
    setError(null);
    try {
      const { login_url } = await api.loginUrl();
      // Hard navigate — Kite will redirect back to backend /auth/callback,
      // which will then redirect us back to "/" with ?login=success.
      window.location.assign(login_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-2xl">Sign in with Zerodha</CardTitle>
          <CardDescription>
            Authenticate via Kite Connect to start a strategy run. Your session
            stays cached locally until ~6 AM IST tomorrow.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Button onClick={handleLogin} disabled={loading} size="lg" className="w-full">
            <LogIn className="mr-2 h-5 w-5" />
            {loading ? "Redirecting…" : "Login with Kite"}
          </Button>
          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
              <p className="mt-2 text-xs text-muted-foreground">
                Make sure <code>KITE_API_KEY</code> and <code>KITE_API_SECRET</code> are
                set in <code>.env</code> and the redirect URL on developers.kite.trade
                matches <code>http://127.0.0.1:8000/auth/callback</code>.
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
