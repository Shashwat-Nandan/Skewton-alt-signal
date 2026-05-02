import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

const BACKEND = "http://127.0.0.1:8000";

// In dev, proxy API paths to the FastAPI backend so the SPA stays same-origin
// (no CORS preflight, cookies/redirects work cleanly). The backend's CORS
// middleware still protects production deploys behind a different origin.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/auth": BACKEND,
      "/strategies": BACKEND,
      "/runs": BACKEND,
    },
  },
});
