"use client";

import { useCallback, useEffect, useState } from "react";
import {
  cancelOrder,
  closePosition,
  fetchDashboard,
  pauseBot,
  resumeBot,
  startBotEngine,
  stopBotEngine,
} from "@/lib/dashboard-api";
import type { DashboardSnapshot } from "@/lib/dashboard-types";

const POLL_INTERVAL_MS = 15000;

/** All dashboard state and the operator actions that mutate it.
 *
 * Keeping this out of the page means the panels stay pure presentational
 * components: they receive data and callbacks, and never fetch anything.
 *
 * `busyAction` holds the id of the single in-flight action (a control mode, an
 * order id, or a position symbol). Every button disables while it is set, so
 * an operator cannot fire a second start/stop/close before the first resolves.
 */
export function useDashboard() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  const loadSnapshot = useCallback(async () => {
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
  }, []);

  useEffect(() => {
    queueMicrotask(() => void loadSnapshot());
    const timer = window.setInterval(() => {
      void loadSnapshot();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadSnapshot]);

  /** Run one operator action, then refresh so the UI reflects the new truth
   * from the bot rather than an optimistic guess. */
  const runAction = useCallback(
    async (key: string, action: () => Promise<unknown>, failureMessage: string) => {
      setBusyAction(key);
      try {
        await action();
        await loadSnapshot();
      } catch (actionError) {
        setError(actionError instanceof Error ? actionError.message : failureMessage);
      } finally {
        setBusyAction(null);
      }
    },
    [loadSnapshot]
  );

  const refreshNow = useCallback(async () => {
    setLoading(true);
    await loadSnapshot();
  }, [loadSnapshot]);

  return {
    snapshot,
    error,
    loading,
    busyAction,
    lastRefresh,
    refreshNow,
    pause: () => runAction("pause", pauseBot, "control action failed"),
    resume: () => runAction("resume", resumeBot, "control action failed"),
    startEngine: () => runAction("engine-start", startBotEngine, "engine action failed"),
    stopEngine: () => runAction("engine-stop", stopBotEngine, "engine action failed"),
    closePosition: (symbol: string) =>
      runAction(symbol, () => closePosition(symbol), "close action failed"),
    cancelOrder: (id: string) => runAction(id, () => cancelOrder(id), "cancel action failed"),
  };
}
