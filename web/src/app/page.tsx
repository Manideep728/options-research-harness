"use client";

import { useEffect, useState } from "react";
import {
  cancelOrder,
  closePosition,
  fetchDashboard,
  pauseBot,
  resumeBot,
} from "@/lib/dashboard-api";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
} from "@/components/ai-elements/conversation";
import { CodeBlock } from "@/components/ai-elements/code-block";
import {
  Message,
  MessageContent,
} from "@/components/ai-elements/message";
import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from "@/components/ai-elements/reasoning";
import {
  Suggestion,
  Suggestions,
} from "@/components/ai-elements/suggestion";
import {
  Task,
  TaskContent,
  TaskItem,
  TaskItemFile,
  TaskTrigger,
} from "@/components/ai-elements/task";
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
    void loadSnapshot();
    const timer = window.setInterval(() => {
      void loadSnapshot();
    }, 15000);
    return () => window.clearInterval(timer);
  }, []);

  const latestSignal = snapshot?.symbols.find((symbol) => symbol.signal_ready) ?? snapshot?.symbols[0] ?? null;

  const reasoningMarkdown = latestSignal
    ? `### Signal
${latestSignal.reason}

### Gate
${latestSignal.gate_reason}

### Sizing
${latestSignal.size_reason ?? "Waiting for a qualifying contract."}`
    : "No signal data yet.";

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

  const refreshNow = async () => {
    setLoading(true);
    await loadSnapshot();
  };

  const paused = snapshot?.control.paused ?? false;
  const statusClass = error
    ? "border-rose-400/40 bg-rose-400/10 text-rose-100"
    : snapshot
      ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-100"
      : "border-amber-400/40 bg-amber-400/10 text-amber-100";

  return (
    <main className="relative min-h-screen overflow-hidden px-4 py-6 text-slate-100 sm:px-6 lg:px-8">
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6">
        <header className="dashboard-panel relative overflow-hidden rounded-[28px] px-6 py-6 sm:px-8">
          <div className="absolute inset-0 bg-[linear-gradient(135deg,rgba(15,23,42,0.94),rgba(15,23,42,0.76))]" />
          <div className="absolute -left-16 top-0 h-44 w-44 rounded-full bg-cyan-400/10 blur-3xl" />
          <div className="absolute right-0 top-6 h-44 w-44 rounded-full bg-amber-400/10 blur-3xl" />
          <div className="relative flex flex-col gap-6 xl:flex-row xl:items-end xl:justify-between">
            <div className="max-w-3xl space-y-4">
              <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs uppercase tracking-[0.28em] text-slate-300">
                <span className={`h-2.5 w-2.5 rounded-full ${snapshot?.market.is_open ? "bg-emerald-400" : "bg-slate-500"}`} />
                Paper trading control room
              </div>
              <div>
                <h1 className="text-4xl font-semibold tracking-tight text-white sm:text-5xl">
                  Calm, high-contrast visibility into every trade decision.
                </h1>
                <p className="mt-4 max-w-2xl text-base leading-7 text-slate-300 sm:text-lg">{headline}</p>
              </div>
              <div className="flex flex-wrap gap-3 text-sm text-slate-300">
                <Badge tone={snapshot?.bot.dry_run ? "amber" : "cyan"}>
                  {snapshot?.bot.dry_run ? "Dry-run" : "Paper orders"}
                </Badge>
                <Badge tone={paused ? "rose" : "emerald"}>{paused ? "Paused" : "Running"}</Badge>
                <Badge tone={snapshot?.market.is_open ? "emerald" : "slate"}>
                  {snapshot?.market.is_open ? "Market open" : "Market closed"}
                </Badge>
                <Badge tone={snapshot ? "slate" : "amber"}>{error ? "API offline" : "API healthy"}</Badge>
              </div>
            </div>

            <div className="dashboard-panel grid gap-4 rounded-3xl p-4 sm:grid-cols-2 xl:w-[420px]">
              <StatusTile label="Refresh" value={lastRefresh ? formatDateTime(lastRefresh) : "-"} />
              <StatusTile label="Symbols" value={snapshot?.bot.symbols.join(" / ") ?? "-"} />
              <StatusTile label="Loop" value={snapshot ? `${snapshot.bot.loop_interval_sec}s` : "-"} />
              <StatusTile label="Generated" value={snapshot ? formatDateTime(snapshot.generated_at) : "-"} />
            </div>
          </div>
        </header>

        <section className="grid gap-4 xl:grid-cols-6">
          <MetricCard label="Equity" value={snapshot ? formatCurrency(snapshot.day.equity) : "-"} hint="Current paper account equity" accent="from-cyan-400/20 to-cyan-400/5" />
          <MetricCard label="Day P&L" value={snapshot ? formatCurrency(snapshot.day.day_pnl) : "-"} hint={snapshot ? `${formatPercent(snapshot.day.day_pnl_pct)} from day start` : "Waiting for data"} accent="from-amber-400/20 to-amber-400/5" />
          <MetricCard label="Trades today" value={snapshot ? compact.format(snapshot.day.trades_today) : "-"} hint="Broker fills plus working buy orders" accent="from-emerald-400/20 to-emerald-400/5" />
          <MetricCard label="Realized P&L" value={snapshot ? formatCurrency(snapshot.journal.realized_pnl) : "-"} hint={`${snapshot?.journal.wins ?? 0} wins / ${snapshot?.journal.losses ?? 0} losses`} accent="from-fuchsia-400/20 to-fuchsia-400/5" />
          <MetricCard label="Open positions" value={snapshot ? compact.format(snapshot.positions.length) : "-"} hint="Long option positions only" accent="from-sky-400/20 to-sky-400/5" />
          <MetricCard label="Open orders" value={snapshot ? compact.format(snapshot.open_orders.length) : "-"} hint="Working orders seen by the broker" accent="from-rose-400/20 to-rose-400/5" />
        </section>

        <section className="grid gap-6 xl:grid-cols-[1.5fr_0.85fr]">
          <div className="flex flex-col gap-6">
            <section className="dashboard-panel rounded-[28px] p-5 sm:p-6">
              <SectionHeader
                title="Why now"
                subtitle="The current signal, gate, and contract selection are surfaced in plain language."
                action={<RefreshButton loading={loading} onClick={refreshNow} />}
              />
              <div className="mt-5 grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
                <div className="rounded-3xl border border-white/8 bg-slate-950/40 p-5">
                  {latestSignal ? (
                    <div className="space-y-4">
                      <div className="flex flex-wrap items-center gap-3">
                        <Badge tone={toneForAction(latestSignal.action)}>{latestSignal.symbol}</Badge>
                        <span className="text-sm text-slate-400">{latestSignal.action}</span>
                        <span className="text-sm text-slate-500">{latestSignal.trend} trend</span>
                      </div>
                      <p className="text-base leading-7 text-slate-200">{latestSignal.reason}</p>
                      <Reasoning defaultOpen isStreaming={false}>
                        <ReasoningTrigger className="rounded-full border border-white/10 bg-white/5 px-3 py-2 text-xs uppercase tracking-[0.24em] text-slate-300" />
                        <ReasoningContent>{reasoningMarkdown}</ReasoningContent>
                      </Reasoning>
                      <div className="grid gap-3 text-sm text-slate-300 sm:grid-cols-2">
                        <MiniStat label="Gate" value={latestSignal.gate_allowed ? "Allowed" : "Blocked"} detail={latestSignal.gate_reason} />
                        <MiniStat label="Price" value={latestSignal.underlying_price ? formatCurrency(latestSignal.underlying_price) : "Pending"} detail="Underlying spot used for contract screening" />
                        <MiniStat label="Sizing" value={latestSignal.size_allowed === null ? "N/A" : latestSignal.size_allowed ? "Allowed" : "Blocked"} detail={latestSignal.size_reason ?? "Waiting for a qualifying contract"} />
                        <MiniStat label="Signal" value={latestSignal.signal_ready ? "Ready" : "Idle"} detail={latestSignal.contract ? latestSignal.contract.symbol : "No qualifying contract yet"} />
                      </div>
                    </div>
                  ) : (
                    <EmptyState title="Waiting on dashboard data" body="Start the API and the bot, then the signal summary will populate here." />
                  )}
                </div>

                <div className="rounded-3xl border border-white/8 bg-slate-950/40 p-5">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <h3 className="text-lg font-medium text-white">Operator controls</h3>
                      <p className="mt-1 text-sm text-slate-400">Pause or resume new entries without stopping exits.</p>
                    </div>
                    <span className={`rounded-full border px-3 py-1 text-xs ${paused ? "border-rose-400/30 bg-rose-400/10 text-rose-100" : "border-emerald-400/30 bg-emerald-400/10 text-emerald-100"}`}>
                      {paused ? "Paused" : "Running"}
                    </span>
                  </div>

                  <div className="mt-5 flex flex-wrap gap-3">
                    <button
                      type="button"
                      onClick={() => void runControl("pause")}
                      disabled={busyAction !== null || paused}
                      className="rounded-full border border-white/10 bg-white/5 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {busyAction === "pause" ? "Pausing..." : "Pause new entries"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void runControl("resume")}
                      disabled={busyAction !== null || !paused}
                      className="rounded-full bg-[linear-gradient(135deg,#22d3ee,#0ea5e9)] px-4 py-2.5 text-sm font-semibold text-slate-950 transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {busyAction === "resume" ? "Resuming..." : "Resume trading"}
                    </button>
                  </div>

                  <div className="mt-4">
                    <Suggestions>
                      <Suggestion suggestion="Refresh now" onClick={() => void refreshNow()} />
                      <Suggestion suggestion={paused ? "Resume trading" : "Pause new entries"} onClick={() => void runControl(paused ? "resume" : "pause")} />
                    </Suggestions>
                  </div>

                  <div className="mt-5 grid gap-3 text-sm text-slate-300">
                    <MiniStat label="Control state" value={snapshot?.control.updated_at ? formatDateTime(snapshot.control.updated_at) : "Not touched"} detail="Latest pause/resume update" />
                    <MiniStat label="Entry window" value={snapshot?.market.is_open ? "Open" : "Closed"} detail="Exit management remains active" />
                  </div>
                </div>
              </div>
            </section>

            <section className="dashboard-panel rounded-[28px] p-5 sm:p-6">
              <SectionHeader title="Symbol watch" subtitle="Signals, gates, and contract screening for each underlying." />
              <div className="mt-5 grid gap-4">
                {snapshot?.symbols.length ? (
                  snapshot.symbols.map((symbol) => <SymbolRow key={symbol.symbol} symbol={symbol} />)
                ) : (
                  <EmptyState title="No symbol data yet" body="Once the API is online, this section will fill with signal decisions." />
                )}
              </div>
            </section>

            <section className="dashboard-panel rounded-[28px] p-5 sm:p-6">
              <SectionHeader title="Open positions" subtitle="The current long option inventory and the rule that would close each one." />
              <div className="mt-5 overflow-hidden rounded-3xl border border-white/8">
                {snapshot?.positions.length ? (
                  <TableShell headers={["Position", "Entry", "Mark", "P&L", "DTE", "Exit", "Action"]} columns="2fr 1fr 1fr 0.9fr 0.55fr 1.4fr 0.8fr">
                    {snapshot.positions.map((position) => (
                      <PositionRow
                        key={position.symbol}
                        position={position}
                        onClose={async () => {
                          setBusyAction(position.symbol);
                          try {
                            await closePosition(position.symbol);
                            await loadSnapshot();
                          } catch (actionError) {
                            setError(actionError instanceof Error ? actionError.message : "close action failed");
                          } finally {
                            setBusyAction(null);
                          }
                        }}
                        busy={busyAction === position.symbol}
                      />
                    ))}
                  </TableShell>
                ) : (
                  <EmptyState title="No open positions" body="The bot is currently flat or the broker state has not updated yet." />
                )}
              </div>
            </section>

            <section className="dashboard-panel rounded-[28px] p-5 sm:p-6">
              <SectionHeader title="Open orders" subtitle="Working orders and their age so stale orders do not hide in the background." />
              <div className="mt-5 overflow-hidden rounded-3xl border border-white/8">
                {snapshot?.open_orders.length ? (
                  <TableShell headers={["Order", "Side", "Age", "Submitted", "Action"]} columns="2fr 0.6fr 0.6fr 1fr 0.6fr">
                    {snapshot.open_orders.map((order) => (
                      <OrderRow
                        key={order.id}
                        order={order}
                        onCancel={async () => {
                          setBusyAction(order.id);
                          try {
                            await cancelOrder(order.id);
                            await loadSnapshot();
                          } catch (actionError) {
                            setError(actionError instanceof Error ? actionError.message : "cancel action failed");
                          } finally {
                            setBusyAction(null);
                          }
                        }}
                        busy={busyAction === order.id}
                      />
                    ))}
                  </TableShell>
                ) : (
                  <EmptyState title="No open orders" body="If the bot is dry-running, the orders may exist only in logs." />
                )}
              </div>
            </section>
          </div>

          <aside className="flex flex-col gap-6">
            <section className="dashboard-panel rounded-[28px] p-5 sm:p-6">
              <SectionHeader title="Reasoning feed" subtitle="A compact AI-style explanation stream from the latest signals and fills." />
              <div className="mt-5 h-[520px] overflow-hidden rounded-3xl border border-white/8 bg-slate-950/40">
                <Conversation className="h-full">
                  <ConversationContent>
                    {snapshot?.recent_activity.length ? (
                      snapshot.recent_activity.map((item) => (
                        <Message key={`${item.kind}-${item.symbol}-${item.title}`} from="assistant">
                          <MessageContent>
                            <ActivityCard item={item} />
                          </MessageContent>
                        </Message>
                      ))
                    ) : (
                      <ConversationEmptyState
                        title="No activity yet"
                        description="The feed will show signals and fills as soon as the API starts returning live data."
                      />
                    )}
                  </ConversationContent>
                </Conversation>
              </div>
            </section>

            <section className="dashboard-panel rounded-[28px] p-5 sm:p-6">
              <SectionHeader title="Journal" subtitle="What the bot has actually recorded, not just what it expected to do." />
              <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
                <MiniStat label="Fill count" value={snapshot ? compact.format(snapshot.journal.fills) : "-"} detail="Rows written to trades.csv" />
                <MiniStat label="Wins / Losses" value={snapshot ? `${snapshot.journal.wins} / ${snapshot.journal.losses}` : "-"} detail="Derived from realized P&L rows" />
                <MiniStat label="Day start" value={snapshot ? formatCurrency(snapshot.day.day_start_equity) : "-"} detail="Circuit-breaker anchor" />
                <MiniStat label="Generated" value={snapshot ? formatDateTime(snapshot.generated_at) : "-"} detail="Current dashboard payload" />
              </div>

              <Task defaultOpen={false} className="mt-5 rounded-3xl border border-white/8 bg-slate-950/40 p-4">
                <TaskTrigger title="Cycle checklist" />
                <TaskContent>
                  <TaskItem>Pull <TaskItemFile>bars</TaskItemFile> and recompute EMA/RSI.</TaskItem>
                  <TaskItem>Evaluate the trend trigger for each configured symbol.</TaskItem>
                  <TaskItem>Apply the risk gates before touching the option chain.</TaskItem>
                  <TaskItem>Journal fills so realized P&amp;L stays measurable.</TaskItem>
                </TaskContent>
              </Task>

              <div className="mt-5 overflow-hidden rounded-3xl border border-white/8 bg-slate-950/40">
                <CodeBlock code={latestSignalCode} language="json" showLineNumbers className="border-0 bg-transparent" />
              </div>
            </section>

            <section className="dashboard-panel rounded-[28px] p-5 sm:p-6">
              <SectionHeader title="Status" subtitle="Connection health and update cadence." />
              <div className={`mt-5 rounded-3xl border px-4 py-4 text-sm ${statusClass}`}>
                {error ? error : snapshot ? `Live snapshot updated ${lastRefresh ? formatDateTime(lastRefresh) : "just now"}.` : "Loading dashboard snapshot..."}
              </div>
            </section>
          </aside>
        </section>
      </div>
    </main>
  );
}

function Badge({ tone, children }: { tone: "emerald" | "amber" | "cyan" | "rose" | "slate"; children: React.ReactNode }) {
  const toneClasses: Record<typeof tone, string> = {
    emerald: "border-emerald-400/30 bg-emerald-400/10 text-emerald-100",
    amber: "border-amber-400/30 bg-amber-400/10 text-amber-100",
    cyan: "border-cyan-400/30 bg-cyan-400/10 text-cyan-100",
    rose: "border-rose-400/30 bg-rose-400/10 text-rose-100",
    slate: "border-slate-400/20 bg-slate-400/10 text-slate-200",
  };
  return <span className={`rounded-full border px-3 py-1 text-xs font-medium ${toneClasses[tone]}`}>{children}</span>;
}

function toneForAction(action: string): "emerald" | "amber" | "rose" | "slate" {
  if (action === "BUY_CALL") {
    return "emerald";
  }
  if (action === "BUY_PUT") {
    return "rose";
  }
  return "slate";
}

function StatusTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/8 bg-white/5 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.24em] text-slate-400">{label}</div>
      <div className="mt-1 text-sm font-medium text-white">{value}</div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  hint,
  accent,
}: {
  label: string;
  value: string;
  hint: string;
  accent: string;
}) {
  return (
    <div className={`dashboard-panel rounded-[24px] border border-white/8 bg-gradient-to-br ${accent} p-5`}>
      <div className="text-[11px] uppercase tracking-[0.24em] text-slate-400">{label}</div>
      <div className="mt-3 text-3xl font-semibold tracking-tight text-white">{value}</div>
      <div className="mt-2 text-sm leading-6 text-slate-300">{hint}</div>
    </div>
  );
}

function SectionHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-white">{title}</h2>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">{subtitle}</p>
      </div>
      {action}
    </div>
  );
}

function RefreshButton({ loading, onClick }: { loading: boolean; onClick: () => Promise<void> }) {
  return (
    <button
      type="button"
      onClick={() => void onClick()}
      className="rounded-full border border-white/10 bg-white/5 px-4 py-2 text-sm font-medium text-white transition hover:bg-white/10"
    >
      {loading ? "Refreshing..." : "Refresh now"}
    </button>
  );
}

function MiniStat({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-2xl border border-white/8 bg-white/5 p-4">
      <div className="text-[11px] uppercase tracking-[0.24em] text-slate-400">{label}</div>
      <div className="mt-2 text-base font-medium text-white">{value}</div>
      <div className="mt-1 text-sm leading-6 text-slate-400">{detail}</div>
    </div>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-3xl border border-dashed border-white/10 bg-white/5 px-5 py-8 text-center">
      <div className="text-base font-medium text-white">{title}</div>
      <div className="mx-auto mt-2 max-w-lg text-sm leading-6 text-slate-400">{body}</div>
    </div>
  );
}

function SymbolRow({ symbol }: { symbol: SymbolSnapshot }) {
  return (
    <div className="rounded-3xl border border-white/8 bg-slate-950/40 p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={toneForAction(symbol.action)}>{symbol.symbol}</Badge>
            <span className="text-sm text-slate-500">{symbol.action}</span>
            <span className="text-sm text-slate-500">{symbol.trend} trend</span>
          </div>
          <p className="max-w-4xl text-sm leading-6 text-slate-300">{symbol.reason}</p>
        </div>
        <div className="grid gap-2 sm:min-w-[260px]">
          <MiniStat label="Gate" value={symbol.gate_allowed ? "Allowed" : "Blocked"} detail={symbol.gate_reason} />
          <MiniStat label="Sizing" value={symbol.size_allowed === null ? "N/A" : symbol.size_allowed ? "Allowed" : "Blocked"} detail={symbol.size_reason ?? "No contract passed filters"} />
        </div>
      </div>
      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <MiniStat label="Underlying" value={symbol.underlying_price ? formatCurrency(symbol.underlying_price) : "Pending"} detail="Spot price used for screening" />
        <MiniStat label="Contract" value={symbol.contract ? symbol.contract.symbol : "None"} detail={symbol.contract ? `${formatCurrency(symbol.contract.strike)} exp ${formatDateTime(symbol.contract.expiry)}` : "No qualifying chain candidate"} />
        <MiniStat label="Rejections" value={symbol.rejections?.length ? compact.format(symbol.rejections.length) : "0"} detail={symbol.rejections?.[0]?.reason ?? "No filtered contracts yet"} />
      </div>
    </div>
  );
}

function TableShell({
  headers,
  columns,
  children,
}: {
  headers: string[];
  columns: string;
  children: React.ReactNode;
}) {
  return (
    <div className="overflow-hidden">
      <div className="grid gap-0 border-b border-white/8 bg-white/5 px-4 py-3 text-[11px] uppercase tracking-[0.24em] text-slate-400" style={{ gridTemplateColumns: columns }}>
        {headers.map((header) => (
          <div key={header}>{header}</div>
        ))}
      </div>
      <div className="divide-y divide-white/8">{children}</div>
    </div>
  );
}

function PositionRow({
  position,
  onClose,
  busy,
}: {
  position: PositionSnapshot;
  onClose: () => Promise<void>;
  busy: boolean;
}) {
  return (
    <div className="grid items-center gap-0 bg-slate-950/40 px-4 py-4 text-sm text-slate-300" style={{ gridTemplateColumns: "2fr 1fr 1fr 0.9fr 0.55fr 1.4fr 0.8fr" }}>
      <div>
        <div className="font-medium text-white">{position.underlying}</div>
        <div className="text-xs text-slate-500">{position.symbol}</div>
      </div>
      <div>{formatCurrency(position.avg_entry_price)}</div>
      <div>{formatCurrency(position.current_price)}</div>
      <div className={position.pnl_pct >= 0 ? "text-emerald-300" : "text-rose-300"}>{formatPercent(position.pnl_pct)}</div>
      <div>{position.days_to_expiry}</div>
      <div className="text-slate-400">{position.exit_reason ?? "Hold"}</div>
      <div>
        <button
          type="button"
          onClick={() => void onClose()}
          disabled={busy}
          className="rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Closing..." : "Close"}
        </button>
      </div>
    </div>
  );
}

function OrderRow({
  order,
  onCancel,
  busy,
}: {
  order: OrderSnapshot;
  onCancel: () => Promise<void>;
  busy: boolean;
}) {
  return (
    <div className="grid items-center gap-0 bg-slate-950/40 px-4 py-4 text-sm text-slate-300" style={{ gridTemplateColumns: "2fr 0.6fr 0.6fr 1fr 0.6fr" }}>
      <div>
        <div className="font-medium text-white">{order.symbol}</div>
        <div className="text-xs text-slate-500">{order.id}</div>
      </div>
      <div className={order.side === "buy" ? "text-emerald-300" : "text-rose-300"}>{order.side}</div>
      <div>{formatRelativeAge(order.age_sec)}</div>
      <div>{formatDateTime(order.submitted_at)}</div>
      <div>
        <button
          type="button"
          onClick={() => void onCancel()}
          disabled={busy}
          className="rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Canceling..." : "Cancel"}
        </button>
      </div>
    </div>
  );
}

function ActivityCard({ item }: { item: ActivitySnapshot }) {
  return (
    <div className="rounded-3xl border border-white/8 bg-slate-950/40 p-4">
      <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-[0.24em] text-slate-400">
        <span>{item.kind}</span>
        <span className="text-slate-500">{item.symbol}</span>
      </div>
      <div className="mt-2 text-sm font-medium text-white">{item.title}</div>
      <div className="mt-1 text-sm leading-6 text-slate-400">{item.detail}</div>
    </div>
  );
}
