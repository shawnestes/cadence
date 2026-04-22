import { apiFetch } from "@/api/client";
import type {
  TemplateIn,
  TemplateListOut,
  TemplateOut,
  TemplateUpdate,
} from "@/api/types";

export type ListTemplatesArgs = {
  campaign_id: string;
  status?: string;
  tenant?: string;
  limit?: number;
  offset?: number;
};

export function listTemplates(
  args: ListTemplatesArgs,
): Promise<TemplateListOut> {
  const params = new URLSearchParams();
  params.set("campaign_id", args.campaign_id);
  params.set("tenant", args.tenant ?? "otto");
  if (args.status !== undefined) params.set("status", args.status);
  if (args.limit !== undefined) params.set("limit", String(args.limit));
  if (args.offset !== undefined) params.set("offset", String(args.offset));
  return apiFetch<TemplateListOut>(`/api/templates?${params.toString()}`);
}

export function getTemplate(id: string): Promise<TemplateOut> {
  return apiFetch<TemplateOut>(`/api/templates/${encodeURIComponent(id)}`);
}

export function createTemplate(payload: TemplateIn): Promise<TemplateOut> {
  return apiFetch<TemplateOut>("/api/templates", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateTemplate(
  id: string,
  patch: TemplateUpdate,
): Promise<TemplateOut> {
  return apiFetch<TemplateOut>(`/api/templates/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}
