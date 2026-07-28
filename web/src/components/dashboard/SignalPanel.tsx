import {
  Badge,
  EmptyState,
  MiniStat,
  Panel,
  PrimaryButton,
  ReasonLine,
  SecondaryButton,
  SectionHeader,
  toneForAction,
} from "./primitives";
import { formatCurrency, formatDateTime } from "@/lib/format";
import type { SymbolSnapshot } from "@/lib/dashboard-types";

export function SignalPanel({
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
