export interface DashboardSnapshot {
  generated_at: string;
  market: MarketSnapshot;
  bot: BotSnapshot;
  day: DaySnapshot;
  journal: JournalSnapshot;
  control: ControlSnapshot;
  symbols: SymbolSnapshot[];
  positions: PositionSnapshot[];
  open_orders: OrderSnapshot[];
  fills: FillSnapshot[];
  recent_activity: ActivitySnapshot[];
}

export interface MarketSnapshot {
  is_open: boolean;
  now: string;
  next_open: string;
  next_close: string;
}

export interface BotSnapshot {
  dry_run: boolean;
  loop_interval_sec: number;
  symbols: string[];
  // Optional: an older/stale API process may not emit these yet, so consumers
  // must guard against undefined (tsc enforces it) rather than crash.
  active_symbols?: string[];
  ranked_at?: string | null;
  paused: boolean;
  engine_running: boolean;
}

export interface DaySnapshot {
  day: string;
  trades_today: number;
  day_start_equity: number;
  equity: number;
  day_pnl: number;
  day_pnl_pct: number;
}

export interface JournalSnapshot {
  fills: number;
  realized_pnl: number;
  wins: number;
  losses: number;
}

export interface ControlSnapshot {
  paused: boolean;
  updated_at: string;
}

export interface SymbolSnapshot {
  symbol: string;
  action: string;
  reason: string;
  trend: string;
  signal_ready: boolean;
  gate_allowed: boolean;
  gate_reason: string;
  underlying_price: number | null;
  contract: ContractSnapshot | null;
  size_allowed: boolean | null;
  size_reason: string | null;
  rejections: Array<{ contract_symbol: string; reason: string }> | null;
  score?: number | null;       // optional: absent from an older API process
  rank_detail?: string | null;
}

export interface ContractSnapshot {
  symbol: string;
  strike: number;
  expiry: string;
  bid: number;
  ask: number;
  open_interest: number;
}

export interface PositionSnapshot {
  symbol: string;
  underlying: string;
  qty: number;
  avg_entry_price: number;
  current_price: number;
  pnl_pct: number;
  days_to_expiry: number;
  entry_time: string | null;
  exit_reason: string | null;
}

export interface OrderSnapshot {
  id: string;
  symbol: string;
  underlying: string;
  side: string;
  submitted_at: string;
  age_sec: number;
}

export interface FillSnapshot {
  order_id: string;
  filled_at: string;
  symbol: string;
  underlying: string;
  side: string;
  qty: number;
  price: number;
  realized_pnl: string;
}

export interface ActivitySnapshot {
  kind: string;
  symbol: string;
  title: string;
  detail: string;
}