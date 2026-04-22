import { apiFetch } from "@/api/client";
import type {
  WorkflowIn,
  WorkflowListOut,
  WorkflowOut,
  WorkflowUpdate,
} from "@/api/types";

export type ListWorkflowsArgs = {
  tenant?: string;
  status?: string;
  vertical?: string;
  limit?: number;
  offset?: number;
};

export function listWorkflows(
  args: ListWorkflowsArgs = {},
): Promise<WorkflowListOut> {
  const params = new URLSearchParams();
  params.set("tenant", args.tenant ?? "otto");
  if (args.status !== undefined) params.set("status", args.status);
  if (args.vertical !== undefined) params.set("vertical", args.vertical);
  if (args.limit !== undefined) params.set("limit", String(args.limit));
  if (args.offset !== undefined) params.set("offset", String(args.offset));
  return apiFetch<WorkflowListOut>(`/api/workflows?${params.toString()}`);
}

export function getWorkflow(
  id: string,
  tenant = "otto",
): Promise<WorkflowOut> {
  const params = new URLSearchParams({ tenant });
  return apiFetch<WorkflowOut>(
    `/api/workflows/${encodeURIComponent(id)}?${params.toString()}`,
  );
}

export function createWorkflow(payload: WorkflowIn): Promise<WorkflowOut> {
  return apiFetch<WorkflowOut>("/api/workflows", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateWorkflow(
  id: string,
  patch: WorkflowUpdate,
  tenant = "otto",
): Promise<WorkflowOut> {
  const params = new URLSearchParams({ tenant });
  return apiFetch<WorkflowOut>(
    `/api/workflows/${encodeURIComponent(id)}?${params.toString()}`,
    {
      method: "PATCH",
      body: JSON.stringify(patch),
    },
  );
}

export function activateWorkflow(
  id: string,
  tenant = "otto",
): Promise<WorkflowOut> {
  const params = new URLSearchParams({ tenant });
  return apiFetch<WorkflowOut>(
    `/api/workflows/${encodeURIComponent(id)}/activate?${params.toString()}`,
    { method: "POST" },
  );
}

export function pauseWorkflow(
  id: string,
  tenant = "otto",
): Promise<WorkflowOut> {
  const params = new URLSearchParams({ tenant });
  return apiFetch<WorkflowOut>(
    `/api/workflows/${encodeURIComponent(id)}/pause?${params.toString()}`,
    { method: "POST" },
  );
}

export function archiveWorkflow(
  id: string,
  tenant = "otto",
): Promise<WorkflowOut> {
  const params = new URLSearchParams({ tenant });
  return apiFetch<WorkflowOut>(
    `/api/workflows/${encodeURIComponent(id)}/archive?${params.toString()}`,
    { method: "POST" },
  );
}
