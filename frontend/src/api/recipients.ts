import { apiFetch } from "@/api/client";
import type { RecipientIngestIn, RecipientIngestOut } from "@/api/types";

export function ingestRecipient(payload: RecipientIngestIn) {
  return apiFetch<RecipientIngestOut>("/api/ingest/recipients", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
