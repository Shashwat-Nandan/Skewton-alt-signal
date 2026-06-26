import { useEffect } from "react";
import { Routes, Route } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, UnauthorizedError } from "@/lib/api";
import { Header } from "@/components/Header";
import { Home } from "@/pages/Home";
import { ArbitragePage } from "@/pages/ArbitragePage";
import { BuyOnGapPage } from "@/pages/BuyOnGapPage";
import { DashboardLoginPage } from "@/pages/DashboardLoginPage";
import { EquitySwingPage } from "@/pages/EquitySwingPage";
import { KalmanPairsPage } from "@/pages/KalmanPairsPage";
import { MarketProfilePage } from "@/pages/MarketProfilePage";
import { PairCandidatesPage } from "@/pages/PairCandidatesPage";
import { PositionsPage } from "@/pages/PositionsPage";
import { RunPage } from "@/pages/RunPage";
import { Skeleton } from "@/components/ui/skeleton";

export default function App() {
  const qc = useQueryClient();

  // Dashboard-session gate. This runs before everything; if the cookie is
  // missing, expired, or tampered with, the backend returns 401 and we
  // render the login page instead of the app.
  const session = useQuery({
    queryKey: ["session"],
    queryFn: api.sessionStatus,
    retry: (count, e) => !(e instanceof UnauthorizedError) && count < 1,
    staleTime: 30_000,
  });

  // Post-OAuth landing: backend bounces the browser to /?login=success.
  // Force a fresh /auth/status fetch (the cached "not authenticated" answer
  // from before the redirect is now stale) and clean the query out of the URL.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("login") === "success") {
      qc.invalidateQueries({ queryKey: ["auth"] });
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, [qc]);

  if (session.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Skeleton className="h-32 w-80" />
      </div>
    );
  }

  // session.isError covers UnauthorizedError thrown by api.sessionStatus,
  // session.data?.authenticated === false covers a successful 200 with the
  // gate marking us logged out (e.g. after sessionLogout invalidated us).
  if (session.isError || !session.data?.authenticated) {
    return <DashboardLoginPage />;
  }

  return (
    <div className="min-h-screen bg-background">
      <Header />
      <main className="container py-6">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/market-profile" element={<MarketProfilePage />} />
          <Route path="/pair-candidates" element={<PairCandidatesPage />} />
          <Route
            path="/pair-candidates/persistent"
            element={<PairCandidatesPage variant="persistent" />}
          />
          <Route path="/arbitrage" element={<ArbitragePage />} />
          <Route path="/buy-on-gap" element={<BuyOnGapPage />} />
          <Route path="/kalman-pairs" element={<KalmanPairsPage />} />
          <Route path="/equity-swing" element={<EquitySwingPage />} />
        </Routes>
      </main>
    </div>
  );
}
