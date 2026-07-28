/** The narrow right-hand column: operational facts, per-symbol decisions,
 * the activity feed, and the raw payload. */

import { Badge, EmptyState, MiniStat, Panel, SectionHeader, toneForAction, type Tone } from "./primitives";
import { compact, formatCurrency, formatDateTime } from "@/lib/format";
import type { ActivitySnapshot, DashboardSnapshot, SymbolSnapshot } from "@/lib/dashboard-types";

export function SnapshotPanel({ snapshot, statusTone, statusText }: { snapshot: DashboardSnapshot | null; statusTone: Tone; statusText: string }) {
  return (
    <Panel>
      <SectionHeader title="Snapshot" subtitle="Operational facts" />
      <div className="mt-4 grid gap-2">
        <MiniStat label="Fill count" value={snapshot ? compact.format(snapshot.journal.fills) : "-"} detail="Rows written to trades.csv" />
        <MiniStat label="Wins / Losses" value={snapshot ? `${snapshot.journal.wins} / ${snapshot.journal.losses}` : "-"} />
        <MiniStat label="Day start" value={snapshot ? formatCurrency(snapshot.day.day_start_equity) : "-"} detail="Circuit-breaker anchor" />
        <MiniStat label="Generated" value={snapshot ? formatDateTime(snapshot.generated_at) : "-"} />
      </div>
      <div className="mt-4">
        <Badge tone={statusTone}>{statusText}</Badge>
      </div>
    </Panel>
  );
}

export function SymbolWatchPanel({ symbols }: { symbols: SymbolSnapshot[] }) {
  return (
    <Panel>
      <SectionHeader title="Symbol watch" subtitle="Signal decisions per symbol" />
      <div className="mt-4 grid gap-3">
        {symbols.length ? (
          symbols.map((symbol) => <SymbolRow key={symbol.symbol} symbol={symbol} />)
        ) : (
          <EmptyState title="No symbol data yet" body="Once the API is online, this section will fill with signal decisions." />
        )}
      </div>
    </Panel>
  );
}

function SymbolRow({ symbol }: { symbol: SymbolSnapshot }) {
  return (
    <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={toneForAction(symbol.action)}>{symbol.symbol}</Badge>
        <span className="text-xs text-[var(--text-muted)]">{symbol.action}</span>
        <span className="text-xs text-[var(--text-muted)]">{symbol.trend} trend</span>
      </div>
      <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">{symbol.reason}</p>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <MiniStat label="Gate" value={symbol.gate_allowed ? "Allowed" : "Blocked"} tone={symbol.gate_allowed ? "green" : "red"} detail={symbol.gate_reason} />
        <MiniStat label="Sizing" value={symbol.size_allowed === null ? "N/A" : symbol.size_allowed ? "Allowed" : "Blocked"} tone={symbol.size_allowed ? "green" : symbol.size_allowed === false ? "red" : "neutral"} detail={symbol.size_reason ?? "No contract passed filters"} />
      </div>
    </div>
  );
}

export function ActivityPanel({ activity }: { activity: ActivitySnapshot[] }) {
  return (
    <Panel>
      <SectionHeader title="Activity log" subtitle="Signals and fills as they happen" />
      <div className="mt-4 max-h-[360px] overflow-y-auto">
        {activity.length ? (
          <div className="divide-y divide-[var(--border-subtle)]">
            {activity.map((item) => (
              <ActivityRow key={`${item.kind}-${item.symbol}-${item.title}`} item={item} />
            ))}
          </div>
        ) : (
          <EmptyState title="No activity yet" body="The feed will show signals and fills as soon as the API starts returning live data." />
        )}
      </div>
    </Panel>
  );
}

function ActivityRow({ item }: { item: ActivitySnapshot }) {
  return (
    <div className="py-2.5">
      <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-wide text-[var(--text-muted)]">
        <span>{item.kind}</span>
        <span>{item.symbol}</span>
      </div>
      <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">{item.title}</div>
      <div className="mt-0.5 text-sm leading-6 text-[var(--text-muted)]">{item.detail}</div>
    </div>
  );
}

export function RawSignalPanel({ code }: { code: string }) {
  return (
    <Panel>
      <SectionHeader title="Raw signal" subtitle="Latest payload" />
      <pre className="mono mt-4 max-h-[280px] overflow-auto rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-3 text-xs leading-6 text-[var(--text-secondary)]">
        {code}
      </pre>
    </Panel>
  );
}
