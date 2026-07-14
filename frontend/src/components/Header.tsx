import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink } from "react-router-dom";
import {
  LogOut, Menu, X, Activity, BarChart3, Briefcase, CandlestickChart, GitBranch,
  Layers, Scale, Sigma, TrendingDown, TrendingUp,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type NavItem = { to: string; label: string; icon?: LucideIcon; end?: boolean };

// Single source of truth for the nav — rendered inside the hamburger menu.
const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Strategies", end: true },
  { to: "/positions", label: "Positions", icon: Briefcase },
  { to: "/market-profile", label: "Market Profile", icon: BarChart3 },
  { to: "/mp-trend", label: "MP Trend (paper)", icon: CandlestickChart },
  { to: "/pair-candidates", label: "Pair Candidates", icon: GitBranch, end: true },
  { to: "/pair-candidates/persistent", label: "Persistent Pairs", icon: Layers },
  { to: "/equity-swing", label: "Equity Swing", icon: TrendingUp },
  { to: "/arbitrage", label: "Arbitrage", icon: Scale },
  { to: "/buy-on-gap", label: "Buy-on-Gap", icon: TrendingDown },
  { to: "/kalman-pairs", label: "Kalman Pairs", icon: Sigma },
  { to: "/kalman-trend", label: "Kalman Trend (loop)", icon: Activity },
];

export function Header() {
  const qc = useQueryClient();
  const { data: auth } = useQuery({ queryKey: ["auth"], queryFn: api.authStatus });
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close the menu on Escape or an outside click. A document-level listener
  // (not a fixed backdrop) — the header's `backdrop-blur` makes it a containing
  // block for fixed descendants, which would clip an `inset-0` backdrop to the
  // header strip and break click-outside over the page body.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenuOpen(false);
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [menuOpen]);

  // Logout = sign out of the dashboard session (the password gate). The
  // Kite/broker token expires daily on its own and rarely needs explicit
  // teardown, so we don't surface it here. Invalidating ["session"] kicks
  // App.tsx to the login page.
  const logout = useMutation({
    mutationFn: api.sessionLogout,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["session"] }),
  });

  // `relative z-50` on the header: its `backdrop-blur` creates a stacking
  // context, so the dropdown's z-50 is scoped inside it; without lifting the
  // whole header above <main>, page content paints over the open menu.
  return (
    <header className="relative z-50 border-b border-border bg-card/40 backdrop-blur-sm">
      <div className="container flex h-14 items-center justify-between gap-2 sm:gap-4">
        <div className="flex min-w-0 items-center gap-2 sm:gap-3">
          {/* Hamburger — all nav lives behind this. */}
          <div className="relative" ref={menuRef}>
            <Button
              variant="ghost"
              size="sm"
              className="px-2"
              aria-label={menuOpen ? "Close navigation menu" : "Open navigation menu"}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-controls={menuOpen ? "primary-nav" : undefined}
              onClick={() => setMenuOpen((v) => !v)}
            >
              {menuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
            </Button>
            {menuOpen && (
                <nav
                  id="primary-nav"
                  className="absolute left-0 top-full z-50 mt-1 w-56 rounded-md border border-border bg-card p-1 shadow-lg"
                >
                  {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
                    <NavLink
                      key={to}
                      to={to}
                      end={end}
                      onClick={() => setMenuOpen(false)}
                      className={({ isActive }) =>
                        cn(
                          "flex items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-foreground",
                          isActive && "bg-accent/60 text-foreground font-medium",
                        )
                      }
                    >
                      {Icon ? <Icon className="h-4 w-4" /> : <span className="w-4" />}
                      {label}
                    </NavLink>
                  ))}
                </nav>
            )}
          </div>

          <Link to="/" className="flex shrink-0 items-center gap-2 font-semibold">
            <Activity className="h-5 w-5 text-primary" />
            <span className="hidden sm:inline">Strategy Dashboard</span>
            <span className="sm:hidden">Dashboard</span>
            <Badge variant="outline" className="ml-1 hidden text-[10px] sm:inline-flex">
              v0.1
            </Badge>
          </Link>
        </div>
        <div className="flex shrink-0 items-center gap-2 sm:gap-3">
          {auth?.authenticated ? (
            <div className="hidden text-right text-sm leading-tight sm:block">
              <div className="font-medium">{auth.user_name}</div>
              <div className="text-xs text-muted-foreground">{auth.user_id}</div>
            </div>
          ) : (
            <Badge variant="secondary" className="hidden sm:inline-flex">
              Kite not connected
            </Badge>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
            aria-label="Sign out of dashboard"
          >
            <LogOut className="h-4 w-4 sm:mr-2" />
            <span className="hidden sm:inline">Sign out</span>
          </Button>
        </div>
      </div>
    </header>
  );
}
