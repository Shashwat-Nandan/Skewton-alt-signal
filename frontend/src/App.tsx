import { Routes, Route } from "react-router-dom";
import { Header } from "@/components/Header";
import { Home } from "@/pages/Home";
import { RunPage } from "@/pages/RunPage";

export default function App() {
  return (
    <div className="min-h-screen bg-background">
      <Header />
      <main className="container py-6">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/runs/:runId" element={<RunPage />} />
        </Routes>
      </main>
    </div>
  );
}
