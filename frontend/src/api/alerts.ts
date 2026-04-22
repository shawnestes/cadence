import { apiFetch } from "@/api/client";
import type { AlertsListOut, AlertStatus } from "@/api/types";

export function listAlerts({
  tenant = "otto",
  status,
  limit = 50,
  offset = 0,
}: {
  tenant?: string;
  status?: AlertStatus;
  limit?: number;
  offset?: number;
} = {}): Promise<AlertsListOut> {
  const params = new URLSearchParams({
    tenant,
    limit: String(limit),
    offset: String(offset),
  });
  if (status !== undefined) {
    params.set("status", status);
  }
  return apiFetch<AlertsListOut>(`/api/alerts?${params.toString()}`);
}
