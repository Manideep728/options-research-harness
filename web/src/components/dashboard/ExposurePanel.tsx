import { EmptyState, Panel, RowButton, SectionHeader, TableShell } from "./primitives";
import { formatCurrency, formatDateTime, formatPercent, formatRelativeAge } from "@/lib/format";
import type { DashboardSnapshot, OrderSnapshot, PositionSnapshot } from "@/lib/dashboard-types";

const POSITION_COLUMNS = "1.8fr 1fr 1fr 0.9fr 0.5fr 1.6fr 0.7fr";
const ORDER_COLUMNS = "2fr 0.6fr 0.6fr 1fr 0.6fr";

export function ExposurePanel({
  snapshot,
  busyAction,
  onClosePosition,
  onCancelOrder,
}: {
  snapshot: DashboardSnapshot | null;
  busyAction: string | null;
  onClosePosition: (symbol: string) => void;
  onCancelOrder: (id: string) => void;
}) {
  return (
    <Panel>
      <SectionHeader title="Open exposure" subtitle="Positions and working orders" />
      <div className="mt-4 flex flex-col gap-5">
        <div>
          <h3 className="text-xs uppercase tracking-wide text-[var(--text-muted)]">Open positions</h3>
          <div className="mt-2 overflow-hidden rounded-md border border-[var(--border-subtle)]">
            {snapshot?.positions.length ? (
              <TableShell headers={["Position", "Entry", "Mark", "P&L", "DTE", "Exit", ""]} columns={POSITION_COLUMNS}>
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
              <TableShell headers={["Order", "Side", "Age", "Submitted", ""]} columns={ORDER_COLUMNS}>
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

function PositionRow({ position, onClose, busy }: { position: PositionSnapshot; onClose: () => void; busy: boolean }) {
  const positive = position.pnl_pct >= 0;
  return (
    <div
      className="grid items-center gap-1 bg-[var(--surface)] px-3 py-3 text-sm text-[var(--text-secondary)]"
      style={{ gridTemplateColumns: POSITION_COLUMNS }}
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
        <RowButton onClick={onClose} disabled={busy}>
          {busy ? "Closing..." : "Close"}
        </RowButton>
      </div>
    </div>
  );
}

function OrderRow({ order, onCancel, busy }: { order: OrderSnapshot; onCancel: () => void; busy: boolean }) {
  return (
    <div
      className="grid items-center gap-1 bg-[var(--surface)] px-3 py-3 text-sm text-[var(--text-secondary)]"
      style={{ gridTemplateColumns: ORDER_COLUMNS }}
    >
      <div>
        <div className="font-medium text-[var(--text-primary)]">{order.symbol}</div>
        <div className="mono text-xs text-[var(--text-muted)]">{order.id}</div>
      </div>
      <div className={`mono ${order.side === "buy" ? "text-[var(--green)]" : "text-[var(--red)]"}`}>{order.side}</div>
      <div className="mono">{formatRelativeAge(order.age_sec)}</div>
      <div className="mono">{formatDateTime(order.submitted_at)}</div>
      <div>
        <RowButton onClick={onCancel} disabled={busy}>
          {busy ? "Canceling..." : "Cancel"}
        </RowButton>
      </div>
    </div>
  );
}
