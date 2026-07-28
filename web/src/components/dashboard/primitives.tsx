/** Shared presentational primitives for the dashboard.
 *
 * These carry no data-fetching or bot logic — they exist so every panel
 * renders borders, tones, and spacing identically.
 */

export type Tone = "green" | "red" | "neutral";

export function toneForAction(action: string): Tone {
  if (action === "BUY_CALL") return "green";
  if (action === "BUY_PUT") return "red";
  return "neutral";
}

export function Badge({ tone, children }: { tone: Tone; children: React.ReactNode }) {
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

export function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-[var(--border-subtle)] bg-[var(--surface)] p-5 ${className}`}>
      {children}
    </section>
  );
}

export function SectionHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
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

export function PrimaryButton({
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

export function SecondaryButton({
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

export function RowButton({
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
      className="rounded border border-[var(--border-strong)] px-2 py-1 text-xs font-medium text-[var(--text-primary)] transition hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}

export function MiniStat({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail?: string; tone?: Tone }) {
  const valueColor = tone === "green" ? "text-[var(--green)]" : tone === "red" ? "text-[var(--red)]" : "text-[var(--text-primary)]";
  return (
    <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-3">
      <div className="text-[11px] uppercase tracking-wide text-[var(--text-muted)]">{label}</div>
      <div className={`mono mt-1 text-sm font-medium ${valueColor}`}>{value}</div>
      {detail ? <div className="mt-1 text-xs leading-5 text-[var(--text-muted)]">{detail}</div> : null}
    </div>
  );
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-md border border-dashed border-[var(--border-subtle)] px-5 py-8 text-center">
      <div className="text-sm font-medium text-[var(--text-primary)]">{title}</div>
      <div className="mx-auto mt-1 max-w-lg text-sm text-[var(--text-muted)]">{body}</div>
    </div>
  );
}

export function ReasonLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 border-l-2 border-[var(--border-strong)] pl-3">
      <span className="shrink-0 text-xs uppercase tracking-wide text-[var(--text-muted)]">{label}</span>
      <span className="text-sm text-[var(--text-secondary)]">{value}</span>
    </div>
  );
}

export function TableShell({ headers, columns, children }: { headers: string[]; columns: string; children: React.ReactNode }) {
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
