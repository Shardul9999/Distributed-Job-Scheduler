// Small formatters shared across the pages. Kept pure and dependency-free.

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const secs = Math.round((Date.now() - then) / 1000);
  const future = secs < 0;
  const a = Math.abs(secs);
  const unit =
    a < 60
      ? `${a}s`
      : a < 3600
        ? `${Math.round(a / 60)}m`
        : a < 86400
          ? `${Math.round(a / 3600)}h`
          : `${Math.round(a / 86400)}d`;
  return future ? `in ${unit}` : `${unit} ago`;
}

export function durationMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

export function ageSeconds(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  return id.slice(0, 8);
}

export function num(n: number): string {
  return n.toLocaleString();
}
