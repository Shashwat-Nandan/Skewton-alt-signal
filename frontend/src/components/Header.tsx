import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink } from "react-router-dom";
import { LogOut, Activity, BarChart3 } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function Header() {
  const qc = useQueryClient();
  const { data: auth } = useQuery({ queryKey: ["auth"], queryFn: api.authStatus });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth"] }),
  });

  return (
    <header className="border-b border-border bg-card/40 backdrop-blur-sm">
      <div className="container flex h-14 items-center justify-between gap-2 sm:gap-4">
        <div className="flex min-w-0 items-center gap-3 sm:gap-6">
          <Link to="/" className="flex shrink-0 items-center gap-2 font-semibold">
            <Activity className="h-5 w-5 text-primary" />
            <span className="hidden sm:inline">Strategy Dashboard</span>
            <span className="sm:hidden">Dashboard</span>
            <Badge variant="outline" className="ml-1 hidden text-[10px] sm:inline-flex">
              v0.1
            </Badge>
          </Link>
          <nav className="-mx-1 flex min-w-0 items-center gap-1 overflow-x-auto px-1 text-sm">
            <NavLink
              to="/"
              end
              className={({ isActive }) =>
                cn(
                  "shrink-0 rounded-md px-2 py-1 text-muted-foreground hover:text-foreground",
                  isActive && "text-foreground font-medium",
                )
              }
            >
              Strategies
            </NavLink>
            <NavLink
              to="/market-profile"
              className={({ isActive }) =>
                cn(
                  "flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-muted-foreground hover:text-foreground",
                  isActive && "text-foreground font-medium",
                )
              }
            >
              <BarChart3 className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Market Profile</span>
              <span className="sm:hidden">Profile</span>
            </NavLink>
          </nav>
        </div>
        <div className="flex shrink-0 items-center gap-2 sm:gap-3">
          {auth?.authenticated ? (
            <>
              <div className="hidden text-right text-sm leading-tight sm:block">
                <div className="font-medium">{auth.user_name}</div>
                <div className="text-xs text-muted-foreground">{auth.user_id}</div>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => logout.mutate()}
                disabled={logout.isPending}
                aria-label="Logout"
              >
                <LogOut className="h-4 w-4 sm:mr-2" />
                <span className="hidden sm:inline">Logout</span>
              </Button>
            </>
          ) : (
            <Badge variant="secondary">Not signed in</Badge>
          )}
        </div>
      </div>
    </header>
  );
}
