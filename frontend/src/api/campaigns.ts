import { apiFetch } from "@/api/client";
import type {
  CampaignGenerateResult,
  CampaignIn,
  CampaignListOut,
  CampaignOut,
  CampaignSendRequest,
  CampaignSendResult,
  CampaignUpdate,
} from "@/api/types";

export type ListCampaignsArgs = {
  tenant?: string;
  status?: string;
  limit?: number;
  offset?: number;
};

export function listCampaigns(
  args: ListCampaignsArgs = {},
): Promise<CampaignListOut> {
  const params = new URLSearchParams();
  params.set("tenant", args.tenant ?? "otto");
  if (args.status !== undefined) params.set("status", args.status);
  if (args.limit !== undefined) params.set("limit", String(args.limit));
  if (args.offset !== undefined) params.set("offset", String(args.offset));
  return apiFetch<CampaignListOut>(`/api/campaigns?${params.toString()}`);
}

export function getCampaign(id: string): Promise<CampaignOut> {
  return apiFetch<CampaignOut>(`/api/campaigns/${encodeURIComponent(id)}`);
}

export function createCampaign(payload: CampaignIn): Promise<CampaignOut> {
  return apiFetch<CampaignOut>("/api/campaigns", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateCampaign(
  id: string,
  patch: CampaignUpdate,
): Promise<CampaignOut> {
  return apiFetch<CampaignOut>(`/api/campaigns/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function triggerCampaignSend(
  id: string,
  body: CampaignSendRequest,
): Promise<CampaignSendResult> {
  return apiFetch<CampaignSendResult>(
    `/api/campaigns/${encodeURIComponent(id)}/send`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export function triggerCampaignGenerate(
  id: string,
  variantsPerCohort?: number,
): Promise<CampaignGenerateResult> {
  const params = new URLSearchParams();
  if (variantsPerCohort !== undefined) {
    params.set("variants_per_cohort", String(variantsPerCohort));
  }
  const qs = params.toString();
  const url = `/api/campaigns/${encodeURIComponent(id)}/generate${qs ? `?${qs}` : ""}`;
  return apiFetch<CampaignGenerateResult>(url, { method: "POST" });
}
