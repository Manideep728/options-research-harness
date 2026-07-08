"use client";

import { useEffect, useState } from "react";
import {
  cancelOrder,
  closePosition,
  fetchDashboard,
  pauseBot,
  resumeBot,
  startBotEngine,
  stopBotEngine,
} from "@/lib/dashboard-api";
import type {
  ActivitySnapshot,
  DashboardSnapshot,
  OrderSnapshot,
  PositionSnapshot,
  SymbolSnapshot,
} from "@/lib/dashboard-types";

const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const percent = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 1,
});

const compact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 2,
});

function formatCurrency(value: number) {
  return currency.format(value);
}

function formatPercent(value: number) {
  return percent.format(value);
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatRelativeAge(seconds: number) {
  if (seconds < 60) {
    return `${Math.max(1, Math.round(seconds))}s`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m`;
  }
  return `${Math.round(seconds / 3600)}h`;
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  const loadSnapshot = async () => {
    try {
      const data = await fetchDashboard();
      setSnapshot(data);
      setError(null);
      setLastRefresh(new Date().toISOString());
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "dashboard fetch failed");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => void loadSnapshot());
    const timer = window.setInterval(() => {
      void loadSnapshot();
    }, 15000);
    return () => window.clearInterval(timer);
  }, []);

  const latestSignal = snapshot?.symbols.find((symbol) => symbol.signal_ready) ?? snapshot?.symbols[0] ?? null;

  const latestSignalCode = latestSignal
    ? JSON.stringify(
        {
          symbol: latestSignal.symbol,
          action: latestSignal.action,
          trend: latestSignal.trend,
          gate_allowed: latestSignal.gate_allowed,
          gate_reason: latestSignal.gate_reason,
          underlying_price: latestSignal.underlying_price,
          contract: latestSignal.contract,
          size_allowed: latestSignal.size_allowed,
          size_reason: latestSignal.size_reason,
        },
        null,
        2
      )
    : "{}";

  const headline = snapshot
    ? snapshot.bot.paused
      ? "Bot paused - exits still manage open positions, but new entries are blocked."
      : snapshot.market.is_open
        ? snapshot.bot.dry_run
          ? "Dry-run mode is live. Signals and risk checks are running against real market data."
          : "Paper account mode is live. The bot can submit real paper orders when gates pass."
        : "Market is closed. The dashboard is still updating journal and control state."
    : "Loading live bot state...";

  const runControl = async (mode: "pause" | "resume") => {
    setBusyAction(mode);
    try {
      if (mode === "pause") {
        await pauseBot();
      } else {
        await resumeBot();
      }
      await loadSnapshot();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "control action failed");
    } finally {
      setBusyAction(null);
    }
  };

  const runEngine = async (mode: "start" | "stop") => {
    setBusyAction(`engine-${mode}`);
    try {
      if (mode === "start") {
        await startBotEngine();
      } else {
        await stopBotEngine();
      }
      await loadSnapshot();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "engine action failed");
    } finally {
      setBusyAction(null);
    }
  };

  const refreshNow = async () => {
    setLoading(true);
    await loadSnapshot();
  };

  const paused = snapshot?.control.paused ?? false;
  const engineRunning = snapshot?.bot.engine_running ?? false;
  const statusTone: Tone = error ? "red" : snapshot ? "green" : "neutral";
  const statusText = error
    ? error
    : snapshot
      ? `Live snapshot updated ${lastRefresh ? formatDateTime(lastRefresh) : "just now"}.`
      : "Loading dashboard snapshot...";

  return (
    <main className="min-h-screen px-4 py-6 text-[var(--text-primary)] sm:px-6 lg:px-8">
      <div className="mx-auto flex w-full max-w-[1320px] flex-col gap-4">
        <Header snapshot={snapshot} headline={headline} paused={paused} engineRunning={engineRunning} error={error} />
        <MetricsRow snapshot={snapshot} />
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(320px,0.85fr)]">
          <div className="flex flex-col gap-4">
            <SignalPanel
              latestSignal={latestSignal}
              loading={loading}
              onRefresh={refreshNow}
              paused={paused}
              busyAction={busyAction}
              onPause={() => void runControl("pause")}
              onResume={() => void runControl("resume")}
              controlUpdatedAt={snapshot?.control.updated_at ?? null}
              marketOpen={snapshot?.market.is_open ?? false}
              engineRunning={engineRunning}
              onStartEngine={() => void runEngine("start")}
              onStopEngine={() => void runEngine("stop")}
            />
            <ExposurePanel
              snapshot={snapshot}
              busyAction={busyAction}
              onClosePosition={async (symbol) => {
                setBusyAction(symbol);
                try {
                  await closePosition(symbol);
                  await loadSnapshot();
                } catch (actionError) {
                  setError(actionError instanceof Error ? actionError.message : "close action failed");
                } finally {
                  setBusyAction(null);
                }
              }}
              onCancelOrder={async (id) => {
                setBusyAction(id);
                try {
                  await cancelOrder(id);
                  await loadSnapshot();
                } catch (actionError) {
                  setError(actionError instanceof Error ? actionError.message : "cancel action failed");
                } finally {
                  setBusyAction(null);
                }
              }}
            />
          </div>

          <aside className="flex flex-col gap-4">
            <SnapshotPanel snapshot={snapshot} statusTone={statusTone} statusText={statusText} />
            <SymbolWatchPanel symbols={snapshot?.symbols ?? []} />
            <ActivityPanel activity={snapshot?.recent_activity ?? []} />
            <RawSignalPanel code={latestSignalCode} />
          </aside>
        </div>
      </div>
    </main>
  );
}

type Tone = "green" | "red" | "neutral";

function Badge({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  const toneClasses: Record<Tone, string> = {
    green: "border-[var(--green-border)] bg-[var(--green-dim)] text-[var(--green)]",
    red: "border-[var(--red-border)] bg-[var(--red-dim)] text-[var(--red)]",
    neutral: "border-[var(--border-strong)] bg-white/[0.03] text-[var(--text-secondary)]",
  };
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${toneClasses[tone]}`}>
      {children}
    </span>
  );
}

function toneForAction(action: string): Tone {
  if (action === "BUY_CALL") return "green";
  if (action === "BUY_PUT") return "red";
  return "neutral";
}

function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-[var(--border-subtle)] bg-[var(--surface)] p-5 ${className}`}>
      {children}
    </section>
  );
}

function SectionHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-[var(--text-secondary)]">{title}</h2>
        {subtitle ? <p className="mt-1 text-sm text-[var(--text-muted)]">{subtitle}</p> : null}
      </div>
      {action}
    </div>
  );
}

function PrimaryButton({
  children,
  onClick,
  disabled,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md bg-[var(--green)] px-4 py-2 text-sm font-semibold text-[var(--primary-foreground)] transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function SecondaryButton({
  children,
  onClick,
  disabled,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md border border-[var(--border-strong)] bg-white/[0.02] px-4 py-2 text-sm font-medium text-[var(--text-primary)] transition hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function MiniStat({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail?: string; tone?: Tone }) {
  const valueColor = tone === "green" ? "text-[var(--green)]" : tone === "red" ? "text-[var(--red)]" : "text-[var(--text-primary)]";
  return (
    <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-3">
      <div className="text-[11px] uppercase tracking-wide text-[var(--text-muted)]">{label}</div>
      <div className={`mono mt-1 text-sm font-medium ${valueColor}`}>{value}</div>
      {detail ? <div className="mt-1 text-xs leading-5 text-[var(--text-muted)]">{detail}</div> : null}
    </div>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-md border border-dashed border-[var(--border-subtle)] px-5 py-8 text-center">
      <div className="text-sm font-medium text-[var(--text-primary)]">{title}</div>
      <div className="mx-auto mt-1 max-w-lg text-sm text-[var(--text-muted)]">{body}</div>
    </div>
  );
}

function Header({
  snapshot,
  headline,
  paused,
  engineRunning,
  error,
}: {
  snapshot: DashboardSnapshot | null;
  headline: string;
  paused: boolean;
  engineRunning: boolean;
  error: string | null;
}) {
  return (
    <Panel>
      <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl space-y-3">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-[var(--text-muted)]">
            <span className={`h-2 w-2 rounded-full ${snapshot?.market.is_open ? "bg-[var(--green)]" : "bg-[var(--text-muted)]"}`} />
            Paper trading control room
          </div>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-[var(--text-primary)] sm:text-3xl">
              Bot status
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--text-secondary)]">{headline}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge tone={engineRunning ? "green" : "neutral"}>{engineRunning ? "Engine running" : "Engine stopped"}</Badge>
            <Badge tone="neutral">{snapshot?.bot.dry_run ? "Dry-run" : "Paper orders"}</Badge>
            <Badge tone={paused ? "red" : "green"}>{paused ? "Entries paused" : "Entries active"}</Badge>
            <Badge tone={snapshot?.market.is_open ? "green" : "neutral"}>
              {snapshot?.market.is_open ? "Market open" : "Market closed"}
            </Badge>
            <Badge tone={error ? "red" : "green"}>{error ? "API offline" : "API healthy"}</Badge>
          </div>
        </div>

        <div className="grid gap-2 sm:grid-cols-2 lg:w-[380px]">
          <MiniStat label="Symbols" value={snapshot?.bot.symbols.join(" / ") ?? "-"} />
          <MiniStat label="Loop" value={snapshot ? `${snapshot.bot.loop_interval_sec}s` : "-"} />
          <MiniStat label="Generated" value={snapshot ? formatDateTime(snapshot.generated_at) : "-"} />
          <MiniStat label="Symbols tracked" value={snapshot ? compact.format(snapshot.symbols.length) : "-"} />
        </div>
      </div>
    </Panel>
  );
}

function MetricsRow({ snapshot }: { snapshot: DashboardSnapshot | null }) {
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

function SignalPanel({
  latestSignal,
  loading,
  onRefresh,
  paused,
  busyAction,
  onPause,
  onResume,
  controlUpdatedAt,
  marketOpen,
  engineRunning,
  onStartEngine,
  onStopEngine,
}: {
  latestSignal: SymbolSnapshot | null;
  loading: boolean;
  onRefresh: () => Promise<void>;
  paused: boolean;
  busyAction: string | null;
  onPause: () => void;
  onResume: () => void;
  controlUpdatedAt: string | null;
  marketOpen: boolean;
  engineRunning: boolean;
  onStartEngine: () => void;
  onStopEngine: () => void;
}) {
  return (
    <Panel>
      <SectionHeader
        title="Current signal"
        subtitle="Active signal and operator controls"
        action={
          <SecondaryButton onClick={() => void onRefresh()} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </SecondaryButton>
        }
      />
      <div className="mt-4 grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
        <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-4">
          {latestSignal ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={toneForAction(latestSignal.action)}>{latestSignal.symbol}</Badge>
                <span className="text-sm text-[var(--text-muted)]">{latestSignal.action}</span>
                <span className="text-sm text-[var(--text-muted)]">{latestSignal.trend} trend</span>
              </div>
              <p className="text-sm leading-6 text-[var(--text-secondary)]">{latestSignal.reason}</p>

              <div className="space-y-2 text-sm">
                <ReasonLine label="Gate" value={latestSignal.gate_reason} />
                <ReasonLine label="Sizing" value={latestSignal.size_reason ?? "Waiting for a qualifying contract."} />
              </div>

              <div className="grid gap-2 sm:grid-cols-2">
                <MiniStat label="Gate" value={latestSignal.gate_allowed ? "Allowed" : "Blocked"} tone={latestSignal.gate_allowed ? "green" : "red"} detail={latestSignal.gate_reason} />
                <MiniStat label="Price" value={latestSignal.underlying_price ? formatCurrency(latestSignal.underlying_price) : "Pending"} detail="Underlying spot used for screening" />
                <MiniStat label="Sizing" value={latestSignal.size_allowed === null ? "N/A" : latestSignal.size_allowed ? "Allowed" : "Blocked"} tone={latestSignal.size_allowed ? "green" : latestSignal.size_allowed === false ? "red" : "neutral"} detail={latestSignal.size_reason ?? "Waiting for a qualifying contract"} />
                <MiniStat label="Signal" value={latestSignal.signal_ready ? "Ready" : "Idle"} tone={latestSignal.signal_ready ? "green" : "neutral"} detail={latestSignal.contract ? latestSignal.contract.symbol : "No qualifying contract yet"} />
              </div>
            </div>
          ) : (
            <EmptyState title="Waiting on dashboard data" body="Start the API and the bot, then the signal summary will populate here." />
          )}
        </div>

        <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-medium text-[var(--text-primary)]">Bot process</h3>
              <p className="mt-1 text-xs text-[var(--text-muted)]">Start or stop the trading loop (main.py) itself.</p>
            </div>
            <Badge tone={engineRunning ? "green" : "neutral"}>{engineRunning ? "Running" : "Stopped"}</Badge>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            <PrimaryButton onClick={onStartEngine} disabled={busyAction !== null || engineRunning}>
              {busyAction === "engine-start" ? "Starting..." : "Start bot"}
            </PrimaryButton>
            <SecondaryButton onClick={onStopEngine} disabled={busyAction !== null || !engineRunning}>
              {busyAction === "engine-stop" ? "Stopping..." : "Stop bot"}
            </SecondaryButton>
          </div>

          <div className="mt-5 border-t border-[var(--border-subtle)] pt-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-medium text-[var(--text-primary)]">Entry gate</h3>
                <p className="mt-1 text-xs text-[var(--text-muted)]">Pause or resume new entries without stopping exits.</p>
              </div>
              <Badge tone={paused ? "red" : "green"}>{paused ? "Paused" : "Active"}</Badge>
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              <SecondaryButton onClick={onPause} disabled={busyAction !== null || paused}>
                {busyAction === "pause" ? "Pausing..." : "Pause new entries"}
              </SecondaryButton>
              <SecondaryButton onClick={onResume} disabled={busyAction !== null || !paused}>
                {busyAction === "resume" ? "Resuming..." : "Resume trading"}
              </SecondaryButton>
            </div>
          </div>

          <div className="mt-4 grid gap-2">
            <MiniStat label="Control state" value={controlUpdatedAt ? formatDateTime(controlUpdatedAt) : "Not touched"} detail="Latest pause/resume update" />
            <MiniStat label="Entry window" value={marketOpen ? "Open" : "Closed"} tone={marketOpen ? "green" : "neutral"} detail="Exit management remains active" />
          </div>
        </div>
      </div>
    </Panel>
  );
}

function ReasonLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 border-l-2 border-[var(--border-strong)] pl-3">
      <span className="shrink-0 text-xs uppercase tracking-wide text-[var(--text-muted)]">{label}</span>
      <span className="text-sm text-[var(--text-secondary)]">{value}</span>
    </div>
  );
}

function ExposurePanel({
  snapshot,
  busyAction,
  onClosePosition,
  onCancelOrder,
}: {
  snapshot: DashboardSnapshot | null;
  busyAction: string | null;
  onClosePosition: (symbol: string) => Promise<void>;
  onCancelOrder: (id: string) => Promise<void>;
}) {
  return (
    <Panel>
      <SectionHeader title="Open exposure" subtitle="Positions and working orders" />
      <div className="mt-4 flex flex-col gap-5">
        <div>
          <h3 className="text-xs uppercase tracking-wide text-[var(--text-muted)]">Open positions</h3>
          <div className="mt-2 overflow-hidden rounded-md border border-[var(--border-subtle)]">
            {snapshot?.positions.length ? (
              <TableShell headers={["Position", "Entry", "Mark", "P&L", "DTE", "Exit", ""]} columns="1.8fr 1fr 1fr 0.9fr 0.5fr 1.6fr 0.7fr">
                {snapshot.positions.map((position) => (
                  <PositionRow
                    key={position.symbol}
                    position={position}
                    onClose={() => onClosePosition(position.symbol)}
                    busy={busyAction === position.symbol}
                  />
                ))}
              </TableShell>
            ) : (
              <EmptyState title="No open positions" body="The bot is currently flat or the broker state has not updated yet." />
            )}
          </div>
        </div>

        <div>
          <h3 className="text-xs uppercase tracking-wide text-[var(--text-muted)]">Open orders</h3>
          <div className="mt-2 overflow-hidden rounded-md border border-[var(--border-subtle)]">
            {snapshot?.open_orders.length ? (
              <TableShell headers={["Order", "Side", "Age", "Submitted", ""]} columns="2fr 0.6fr 0.6fr 1fr 0.6fr">
                {snapshot.open_orders.map((order) => (
                  <OrderRow key={order.id} order={order} onCancel={() => onCancelOrder(order.id)} busy={busyAction === order.id} />
                ))}
              </TableShell>
            ) : (
              <EmptyState title="No open orders" body="If the bot is dry-running, the orders may exist only in logs." />
            )}
          </div>
        </div>
      </div>
    </Panel>
  );
}

function TableShell({ headers, columns, children }: { headers: string[]; columns: string; children: React.ReactNode }) {
  return (
    <div>
      <div
        className="grid border-b border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-2 text-[11px] uppercase tracking-wide text-[var(--text-muted)]"
        style={{ gridTemplateColumns: columns }}
      >
        {headers.map((header) => (
          <div key={header}>{header}</div>
        ))}
      </div>
      <div className="divide-y divide-[var(--border-subtle)]">{children}</div>
    </div>
  );
}

function PositionRow({ position, onClose, busy }: { position: PositionSnapshot; onClose: () => void; busy: boolean }) {
  const positive = position.pnl_pct >= 0;
  return (
    <div
      className="grid items-center gap-1 bg-[var(--surface)] px-3 py-3 text-sm text-[var(--text-secondary)]"
      style={{ gridTemplateColumns: "1.8fr 1fr 1fr 0.9fr 0.5fr 1.6fr 0.7fr" }}
    >
      <div className="min-w-0">
        <div className="truncate font-medium text-[var(--text-primary)]">{position.underlying}</div>
        <div className="mono truncate text-xs text-[var(--text-muted)]">{position.symbol}</div>
      </div>
      <div className="mono truncate">{formatCurrency(position.avg_entry_price)}</div>
      <div className="mono truncate">{formatCurrency(position.current_price)}</div>
      <div className={`mono truncate ${positive ? "text-[var(--green)]" : "text-[var(--red)]"}`}>{formatPercent(position.pnl_pct)}</div>
      <div className="mono truncate">{position.days_to_expiry}</div>
      <div className="min-w-0 truncate text-[var(--text-muted)]" title={position.exit_reason ?? "Hold"}>{position.exit_reason ?? "Hold"}</div>
      <div>
        <button
          type="button"
          onClick={onClose}
          disabled={busy}
          className="rounded border border-[var(--border-strong)] px-2 py-1 text-xs font-medium text-[var(--text-primary)] transition hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "Closing..." : "Close"}
        </button>
      </div>
    </div>
  );
}

function OrderRow({ order, onCancel, busy }: { order: OrderSnapshot; onCancel: () => void; busy: boolean }) {
  return (
    <div
      className="grid items-center gap-1 bg-[var(--surface)] px-3 py-3 text-sm text-[var(--text-secondary)]"
      style={{ gridTemplateColumns: "2fr 0.6fr 0.6fr 1fr 0.6fr" }}
    >
      <div>
        <div className="font-medium text-[var(--text-primary)]">{order.symbol}</div>
        <div className="mono text-xs text-[var(--text-muted)]">{order.id}</div>
      </div>
      <div className={`mono ${order.side === "buy" ? "text-[var(--green)]" : "text-[var(--red)]"}`}>{order.side}</div>
      <div className="mono">{formatRelativeAge(order.age_sec)}</div>
      <div className="mono">{formatDateTime(order.submitted_at)}</div>
      <div>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="rounded border border-[var(--border-strong)] px-2 py-1 text-xs font-medium text-[var(--text-primary)] transition hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "Canceling..." : "Cancel"}
        </button>
      </div>
    </div>
  );
}

function SnapshotPanel({ snapshot, statusTone, statusText }: { snapshot: DashboardSnapshot | null; statusTone: Tone; statusText: string }) {
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

function SymbolWatchPanel({ symbols }: { symbols: SymbolSnapshot[] }) {
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

function ActivityPanel({ activity }: { activity: ActivitySnapshot[] }) {
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

function RawSignalPanel({ code }: { code: string }) {
  return (
    <Panel>
      <SectionHeader title="Raw signal" subtitle="Latest payload" />
      <pre className="mono mt-4 max-h-[280px] overflow-auto rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-3 text-xs leading-6 text-[var(--text-secondary)]">
        {code}
      </pre>
    </Panel>
  );
}
