import type { ParamSpec } from "@/lib/types";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Props = {
  params: ParamSpec[];
  values: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
};

export function ParamForm({ params, values, onChange }: Props) {
  if (params.length === 0) {
    return <p className="text-sm text-muted-foreground">No tunable parameters.</p>;
  }

  return (
    <div className="space-y-3">
      {params.map((p) => {
        const id = `param-${p.name}`;
        const placeholder =
          p.default == null ? "(auto)" : String(p.default);
        return (
          <div key={p.name} className="space-y-1">
            <div className="flex items-baseline justify-between">
              <Label htmlFor={id} className="font-mono text-xs">
                {p.name}
              </Label>
              <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {p.type}
              </span>
            </div>
            <Input
              id={id}
              type={p.type === "float" || p.type === "int" ? "number" : "text"}
              step={p.type === "float" ? "any" : undefined}
              placeholder={placeholder}
              value={values[p.name] ?? ""}
              onChange={(e) => onChange({ ...values, [p.name]: e.target.value })}
            />
            <p className="text-xs text-muted-foreground">{p.description}</p>
          </div>
        );
      })}
    </div>
  );
}

/** Coerces the string-form values into the typed payload the backend expects. */
export function coerceParams(
  params: ParamSpec[],
  raw: Record<string, string>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of params) {
    const v = raw[p.name];
    if (v == null || v === "") continue; // skip blanks → backend uses default/auto
    if (p.type === "float") {
      const n = Number(v);
      if (!Number.isNaN(n)) out[p.name] = n;
    } else if (p.type === "int") {
      const n = parseInt(v, 10);
      if (!Number.isNaN(n)) out[p.name] = n;
    } else if (p.type === "bool") {
      out[p.name] = v === "true" || v === "1";
    } else {
      out[p.name] = v;
    }
  }
  return out;
}
