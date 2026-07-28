"use client";

import { ExposurePanel } from "@/components/dashboard/ExposurePanel";
import { Header } from "@/components/dashboard/Header";
import { MetricsRow } from "@/components/dashboard/MetricsRow";
import type { Tone } from "@/components/dashboard/primitives";
import {
  ActivityPanel,
  RawSignalPanel,
  SnapshotPanel,
  SymbolWatchPanel,
} from "@/components/dashboard/SidePanels";
import { SignalPanel } from "@/components/dashboard/SignalPanel";
import { useDashboard } from "@/hooks/use-dashboard";
import { formatDateTime } from "@/lib/format";
import type { DashboardSnapshot, SymbolSnapshot } from "@/lib/dashboard-types";

/** One-line summary of what the bot is doing right now, in operator terms. */
function buildHeadline(snapshot: DashboardSnapshot | null): string {
  if (!snapshot) return "Loading live bot state...";
  if (snapshot.bot.paused) {
    return "Bot paused - exits still manage open positions, but new entries are blocked.";
  }
  if (!snapshot.market.is_open) {
    return "Market is closed. The dashboard is still updating journal and control state.";
  }
  return snapshot.bot.dry_run
    ? "Dry-run mode is live. Signals and risk checks are running against real market data."
    : "Paper account mode is live. The bot can submit real paper orders when gates pass.";
}

/** The raw payload panel shows the signal the operator most needs to see:
 * the first one actually ready to fire, falling back to the first polled. */
function pickLatestSignal(snapshot: DashboardSnapshot | null): SymbolSnapshot | null {
  return snapshot?.symbols.find((symbol) => symbol.signal_ready) ?? snapshot?.symbols[0] ?? null;
}

function toSignalCode(signal: SymbolSnapshot | null): string {
  if (!signal) return "{}";
  return JSON.stringify(
    {
      symbol: signal.symbol,
      action: signal.action,
      trend: signal.trend,
      gate_allowed: signal.gate_allowed,
      gate_reason: signal.gate_reason,
      underlying_price: signal.underlying_price,
      contract: signal.contract,
      size_allowed: signal.size_allowed,
      size_reason: signal.size_reason,
    },
    null,
    2
  );
}

export default function Home() {
  const {
    snapshot,
    error,
    loading,
    busyAction,
    lastRefresh,
    refreshNow,
    pause,
    resume,
    startEngine,
    stopEngine,
    closePosition,
    cancelOrder,
  } = useDashboard();

  const latestSignal = pickLatestSignal(snapshot);
  const headline = buildHeadline(snapshot);
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
              onPause={() => void pause()}
              onResume={() => void resume()}
              controlUpdatedAt={snapshot?.control.updated_at ?? null}
              marketOpen={snapshot?.market.is_open ?? false}
              engineRunning={engineRunning}
              onStartEngine={() => void startEngine()}
              onStopEngine={() => void stopEngine()}
            />
            <ExposurePanel
              snapshot={snapshot}
              busyAction={busyAction}
              onClosePosition={(symbol) => void closePosition(symbol)}
              onCancelOrder={(id) => void cancelOrder(id)}
            />
          </div>

          <aside className="flex flex-col gap-4">
            <SnapshotPanel snapshot={snapshot} statusTone={statusTone} statusText={statusText} />
            <SymbolWatchPanel symbols={snapshot?.symbols ?? []} />
            <ActivityPanel activity={snapshot?.recent_activity ?? []} />
            <RawSignalPanel code={toSignalCode(latestSignal)} />
          </aside>
        </div>
      </div>
    </main>
  );
}
