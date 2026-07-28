/** Display formatters shared by every dashboard panel.
 *
 * Intl formatters are comparatively expensive to construct, so they are built
 * once at module scope and reused rather than re-created on each render.
 */

const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const percent = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 1,
});

const dateTime = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

export const compact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 2,
});

export function formatCurrency(value: number) {
  return currency.format(value);
}

export function formatPercent(value: number) {
  return percent.format(value);
}

export function formatDateTime(value: string) {
  return dateTime.format(new Date(value));
}

/** Compact age for order rows: "45s", "12m", "3h". */
export function formatRelativeAge(seconds: number) {
  if (seconds < 60) {
    return `${Math.max(1, Math.round(seconds))}s`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m`;
  }
  return `${Math.round(seconds / 3600)}h`;
}
