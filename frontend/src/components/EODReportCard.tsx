import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type Props = { report: Record<string, unknown> | null | undefined };

export function EODReportCard({ report }: Props) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Strategy snapshot</CardTitle>
      </CardHeader>
      <CardContent>
        {report ? (
          <pre className="overflow-auto rounded-md bg-muted/50 p-3 text-xs leading-relaxed font-mono">
            {JSON.stringify(report, null, 2)}
          </pre>
        ) : (
          <p className="text-sm text-muted-foreground">No report yet — strategy is warming up.</p>
        )}
      </CardContent>
    </Card>
  );
}
