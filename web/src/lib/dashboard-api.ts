import type { DashboardSnapshot } from "@/lib/dashboard-types";

const API_BASE =
  process.env.NEXT_PUBLIC_DASHBOARD_API_URL ?? "http://127.0.0.1:8000";

async function request(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
}

export async function fetchDashboard(): Promise<DashboardSnapshot> {
  const response = await request("/dashboard");
  if (!response.ok) {
    throw new Error(`dashboard request failed: ${response.status}`);
  }
  return response.json();
}

export async function pauseBot(): Promise<void> {
  const response = await request("/control/pause", { method: "POST" });
  if (!response.ok) {
    throw new Error(`pause request failed: ${response.status}`);
  }
}

export async function resumeBot(): Promise<void> {
  const response = await request("/control/resume", { method: "POST" });
  if (!response.ok) {
    throw new Error(`resume request failed: ${response.status}`);
  }
}

export async function cancelOrder(orderId: string): Promise<void> {
  const response = await request(`/orders/${encodeURIComponent(orderId)}/cancel`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(`cancel request failed: ${response.status}`);
  }
}

export async function closePosition(symbol: string): Promise<void> {
  const response = await request(`/positions/${encodeURIComponent(symbol)}/close`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(`close request failed: ${response.status}`);
  }
}