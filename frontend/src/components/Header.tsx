import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { LogOut, Activity } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export function Header() {
  const qc = useQueryClient();
  const { data: auth } = useQuery({ queryKey: ["auth"], queryFn: api.authStatus });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth"] }),
  });

  return (
    <header className="border-b border-border bg-card/40 backdrop-blur-sm">
      <div className="container flex h-14 items-center justify-between">
        <Link to="/" className="flex items-center gap-2 font-semibold">
          <Activity className="h-5 w-5 text-primary" />
          <span>Strategy Dashboard</span>
          <Badge variant="outline" className="ml-2 text-[10px]">v0.1</Badge>
        </Link>
        <div className="flex items-center gap-3">
          {auth?.authenticated ? (
            <>
              <div className="text-right text-sm leading-tight">
                <div className="font-medium">{auth.user_name}</div>
                <div className="text-xs text-muted-foreground">{auth.user_id}</div>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => logout.mutate()}
                disabled={logout.isPending}
              >
                <LogOut className="mr-2 h-4 w-4" />
                Logout
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
