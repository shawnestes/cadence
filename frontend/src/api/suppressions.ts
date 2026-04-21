import { apiFetch } from "@/api/client";
import type {
  SuppressionIn,
  SuppressionListOut,
  SuppressionOut,
} from "@/api/types";

export type ListSuppressionsParams = {
  tenant?: string;
  email?: string;
  limit?: number;
  offset?: number;
};

export function listSuppressions(
  params: ListSuppressionsParams = {},
): Promise<SuppressionListOut> {
  const qs = new URLSearchParams();
  if (params.tenant) qs.set("tenant", params.tenant);
  if (params.email) qs.set("email", params.email);
  if (params.limit !== undefined) qs.set("limit", String(params.limit));
  if (params.offset !== undefined) qs.set("offset", String(params.offset));
  const query = qs.toString();
  const path = query ? `/api/suppressions?${query}` : "/api/suppressions";
  return apiFetch<SuppressionListOut>(path);
}

export function createSuppression(
  payload: SuppressionIn,
): Promise<SuppressionOut> {
  return apiFetch<SuppressionOut>("/api/suppressions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteSuppression(
  email: string,
  tenant?: string,
): Promise<void> {
  const qs = tenant ? `?tenant=${encodeURIComponent(tenant)}` : "";
  return apiFetch<void>(
    `/api/suppressions/${encodeURIComponent(email)}${qs}`,
    { method: "DELETE" },
  );
}
