import { Badge, MiniStat, Panel } from "./primitives";
import { compact, formatDateTime } from "@/lib/format";
import type { DashboardSnapshot } from "@/lib/dashboard-types";

export function Header({
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
          <MiniStat
            label="Watching"
            value={snapshot?.bot.active_symbols?.length ? snapshot.bot.active_symbols.join(" / ") : "-"}
          />
          <MiniStat label="Loop" value={snapshot ? `${snapshot.bot.loop_interval_sec}s` : "-"} />
          <MiniStat label="Generated" value={snapshot ? formatDateTime(snapshot.generated_at) : "-"} />
          <MiniStat label="Universe" value={snapshot ? compact.format(snapshot.bot.symbols.length) : "-"} />
        </div>
      </div>
    </Panel>
  );
}
