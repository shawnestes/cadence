import { apiFetch } from "@/api/client";
import type {
  RecipientDetail,
  RecipientIngestIn,
  RecipientIngestOut,
  RecipientSearchOut,
} from "@/api/types";

export function ingestRecipient(payload: RecipientIngestIn) {
  return apiFetch<RecipientIngestOut>("/api/ingest/recipients", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export type SearchRecipientsArgs = {
  email?: string;
  tenant?: string;
  limit?: number;
  offset?: number;
};

export function searchRecipients(
  args: SearchRecipientsArgs = {},
): Promise<RecipientSearchOut> {
  const params = new URLSearchParams();
  if (args.email !== undefined && args.email !== "") params.set("email", args.email);
  params.set("tenant", args.tenant ?? "otto");
  if (args.limit !== undefined) params.set("limit", String(args.limit));
  if (args.offset !== undefined) params.set("offset", String(args.offset));
  return apiFetch<RecipientSearchOut>(`/api/recipients?${params.toString()}`);
}

export function getRecipient(
  id: string,
  tenant = "otto",
): Promise<RecipientDetail> {
  const params = new URLSearchParams({ tenant });
  return apiFetch<RecipientDetail>(
    `/api/recipients/${encodeURIComponent(id)}?${params.toString()}`,
  );
}
