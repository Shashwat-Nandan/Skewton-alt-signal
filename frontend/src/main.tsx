import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UnauthorizedError } from "./lib/api";
import App from "./App";
import "./index.css";

const queryClient = new QueryClient({
  // Any 401 anywhere — even a route the user has been on for an hour —
  // marks the session query stale so App.tsx flips to the login page.
  // Saves us from sprinkling auth handling through every component.
  queryCache: new QueryCache({
    onError: (err, query) => {
      if (err instanceof UnauthorizedError && query.queryKey[0] !== "session") {
        queryClient.invalidateQueries({ queryKey: ["session"] });
      }
    },
  }),
  defaultOptions: {
    queries: {
      // Don't retry on 401 — re-fetching the same expired-cookie call is
      // pointless and just spams the backend.
      retry: (count, e) => !(e instanceof UnauthorizedError) && count < 1,
      refetchOnWindowFocus: false,
      staleTime: 1000,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
