import { useEffect } from "react";
import { Routes, Route } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Header } from "@/components/Header";
import { Home } from "@/pages/Home";
import { MarketProfilePage } from "@/pages/MarketProfilePage";
import { PairCandidatesPage } from "@/pages/PairCandidatesPage";
import { RunPage } from "@/pages/RunPage";

export default function App() {
  const qc = useQueryClient();

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

  return (
    <div className="min-h-screen bg-background">
      <Header />
      <main className="container py-6">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/market-profile" element={<MarketProfilePage />} />
          <Route path="/pair-candidates" element={<PairCandidatesPage />} />
        </Routes>
      </main>
    </div>
  );
}
