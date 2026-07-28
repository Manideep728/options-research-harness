import type { Tone } from "./primitives";
import { compact, formatCurrency, formatPercent } from "@/lib/format";
import type { DashboardSnapshot } from "@/lib/dashboard-types";

export function MetricsRow({ snapshot }: { snapshot: DashboardSnapshot | null }) {
  const dayPnlTone: Tone = !snapshot ? "neutral" : snapshot.day.day_pnl >= 0 ? "green" : "red";
  const realizedTone: Tone = !snapshot ? "neutral" : snapshot.journal.realized_pnl >= 0 ? "green" : "red";
  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard label="Equity" value={snapshot ? formatCurrency(snapshot.day.equity) : "-"} hint="Current paper account equity" />
      <MetricCard
        label="Day P&L"
        value={snapshot ? formatCurrency(snapshot.day.day_pnl) : "-"}
        hint={snapshot ? `${formatPercent(snapshot.day.day_pnl_pct)} from day start` : "Waiting for data"}
        tone={dayPnlTone}
      />
      <MetricCard label="Trades today" value={snapshot ? compact.format(snapshot.day.trades_today) : "-"} hint="Broker fills plus working buy orders" />
      <MetricCard
        label="Realized P&L"
        value={snapshot ? formatCurrency(snapshot.journal.realized_pnl) : "-"}
        hint={`${snapshot?.journal.wins ?? 0} wins / ${snapshot?.journal.losses ?? 0} losses`}
        tone={realizedTone}
      />
    </section>
  );
}

function MetricCard({ label, value, hint, tone = "neutral" }: { label: string; value: string; hint: string; tone?: Tone }) {
  const valueColor = tone === "green" ? "text-[var(--green)]" : tone === "red" ? "text-[var(--red)]" : "text-[var(--text-primary)]";
  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--surface)] p-4">
      <div className="text-[11px] uppercase tracking-wide text-[var(--text-muted)]">{label}</div>
      <div className={`mono mt-2 text-2xl font-semibold tracking-tight ${valueColor}`}>{value}</div>
      <div className="mt-1 text-xs leading-5 text-[var(--text-muted)]">{hint}</div>
    </div>
  );
}
