import type { CompositeProfile, DayProfile, ProfileBin } from "@/lib/types";

/**
 * Horizontal Market Profile chart.
 *
 * One row per price bin. Bin width is proportional to TPO count, with the
 * Value Area (VAH/VAL band) shaded and POC highlighted. When `letters` are
 * present (single-day mode), they're drawn left-to-right inside the row,
 * recreating the canonical CBOT TPO display.
 *
 * Pure SVG, no Recharts — the chart is a histogram with text labels which
 * Recharts handles awkwardly. Hand-rolled keeps it tight.
 */

type Source = CompositeProfile | DayProfile;

export function MarketProfileChart({
  profile,
  height = 600,
  showLetters = true,
}: {
  profile: Source;
  height?: number;
  showLetters?: boolean;
}) {
  const bins = profile.bins;
  if (!bins.length) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        No bins to display.
      </div>
    );
  }

  const maxCount = Math.max(...bins.map((b) => b.tpo_count), 1);
  const rowH = Math.max(Math.floor(height / bins.length), 6);
  const totalH = rowH * bins.length;

  // Layout
  const priceColW = 64;
  const tpoColW = 52;
  const histColW = 460;
  const W = priceColW + tpoColW + histColW + 24;

  return (
    <div className="overflow-x-auto">
      <svg width={W} height={totalH + 24} className="font-mono">
        {/* Header */}
        <text x={0} y={12} className="fill-muted-foreground text-[10px]">
          Price
        </text>
        <text x={priceColW} y={12} className="fill-muted-foreground text-[10px]">
          TPOs
        </text>
        <text
          x={priceColW + tpoColW}
          y={12}
          className="fill-muted-foreground text-[10px]"
        >
          Profile
        </text>

        <g transform="translate(0, 16)">
          {bins.map((b, i) => (
            <ProfileRow
              key={`${b.price_mid}-${i}`}
              bin={b}
              y={i * rowH}
              rowH={rowH}
              priceColW={priceColW}
              tpoColW={tpoColW}
              histColW={histColW}
              maxCount={maxCount}
              showLetters={showLetters}
            />
          ))}
        </g>
      </svg>
    </div>
  );
}

function ProfileRow({
  bin,
  y,
  rowH,
  priceColW,
  tpoColW,
  histColW,
  maxCount,
  showLetters,
}: {
  bin: ProfileBin;
  y: number;
  rowH: number;
  priceColW: number;
  tpoColW: number;
  histColW: number;
  maxCount: number;
  showLetters: boolean;
}) {
  const w = (bin.tpo_count / maxCount) * histColW;
  const fill = bin.is_poc
    ? "hsl(var(--primary))"
    : bin.in_value_area
    ? "hsl(var(--primary) / 0.30)"
    : "hsl(var(--muted-foreground) / 0.18)";

  // Render letters only for narrow rows with letters available (daily mode).
  // For composites we have empty strings — hist bar alone is enough.
  const showText = showLetters && bin.letters && rowH >= 11;
  const fontSize = Math.min(rowH - 2, 11);

  return (
    <g transform={`translate(0, ${y})`}>
      <text
        x={priceColW - 4}
        y={rowH * 0.75}
        className="fill-foreground text-[10px]"
        textAnchor="end"
      >
        {formatPrice(bin.price_mid)}
      </text>
      <text
        x={priceColW + tpoColW - 4}
        y={rowH * 0.75}
        className="fill-muted-foreground text-[10px]"
        textAnchor="end"
      >
        {bin.tpo_count}
      </text>
      <rect
        x={priceColW + tpoColW}
        y={1}
        width={Math.max(w, bin.tpo_count > 0 ? 2 : 0)}
        height={rowH - 2}
        fill={fill}
        rx={1}
      />
      {showText && (
        <text
          x={priceColW + tpoColW + 4}
          y={rowH * 0.78}
          className="fill-foreground"
          fontSize={fontSize}
          fontFamily="ui-monospace, Menlo, monospace"
        >
          {bin.letters}
        </text>
      )}
    </g>
  );
}

function formatPrice(p: number): string {
  if (p >= 1000) return p.toFixed(1);
  if (p >= 100) return p.toFixed(2);
  return p.toFixed(2);
}
