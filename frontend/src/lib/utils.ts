import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn's standard className combinator. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatINR(n: number | null | undefined, fractionDigits = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  })}`;
}

export function formatNum(n: number | null | undefined, fractionDigits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-IN", { hour12: false });
}

export function shortId(id: string, n = 8): string {
  return id.slice(0, n);
}

/** True if the current wall-clock time is inside NSE cash-market hours
 * (Mon–Fri, 09:15–15:30 IST). Computed in IST regardless of the user's TZ
 * so the result matches the runners that drive the state files. */
export function isMarketHoursIST(now: Date = new Date()): boolean {
  // Convert "now" to the IST wall-clock by formatting and re-parsing parts.
  // toLocaleString with a forced TZ avoids needing a tz library.
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const weekday = get("weekday"); // Mon, Tue, ...
  const hour = Number(get("hour"));
  const minute = Number(get("minute"));
  if (["Sat", "Sun"].includes(weekday)) return false;
  const mins = hour * 60 + minute;
  return mins >= 9 * 60 + 15 && mins <= 15 * 60 + 30;
}
