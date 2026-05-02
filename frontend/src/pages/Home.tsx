import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { LoginCard } from "@/components/LoginCard";
import { StrategyForm } from "@/components/StrategyForm";
import { RunsList } from "@/components/RunsList";
import { Skeleton } from "@/components/ui/skeleton";

export function Home() {
  const { data: auth, isLoading } = useQuery({
    queryKey: ["auth"],
    queryFn: api.authStatus,
  });

  if (isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-2">
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
      </div>
    );
  }

  if (!auth?.authenticated) {
    return <LoginCard />;
  }

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <StrategyForm />
      <RunsList />
    </div>
  );
}
