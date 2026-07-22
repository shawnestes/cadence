import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getCampaign,
  triggerCampaignGenerate,
  triggerCampaignSend,
  updateCampaign,
} from "@/api/campaigns";
import { listTemplates, updateTemplate } from "@/api/templates";
import type {
  CampaignGenerateResult,
  CampaignSendResult,
  CampaignStatus,
  CampaignUpdate,
  Template,
  TemplateStatus,
  TemplateUpdate,
} from "@/api/types";
import { Badge, statusVariant } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Table, Tbody, Td, Th, Thead, Tr } from "@/components/ui/Table";
import { Textarea } from "@/components/ui/Textarea";

const TEMPLATE_STATUS_OPTIONS = [
  { label: "All", value: "" },
  { label: "generated", value: "generated" },
  { label: "approved", value: "approved" },
  { label: "rejected", value: "rejected" },
  { label: "retired", value: "retired" },
];

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function prettyJson(v: unknown): string {
  if (v == null) return "—";
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function truncate(s: string, n = 60): string {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n)}...` : s;
}

function parseEmails(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function CampaignDetail() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();

  const [templateStatus, setTemplateStatus] = useState("");
  const [variantsPerCohort, setVariantsPerCohort] = useState(5);
  const [emailsRaw, setEmailsRaw] = useState("");
  const [generateResult, setGenerateResult] =
    useState<CampaignGenerateResult | null>(null);
  const [sendResult, setSendResult] = useState<CampaignSendResult | null>(null);
  const [previewTemplate, setPreviewTemplate] = useState<Template | null>(null);
  const [previewTab, setPreviewTab] = useState<"html" | "text">("html");

  const campaignQuery = useQuery({
    queryKey: ["campaign", id],
    queryFn: () => getCampaign(id!),
    enabled: !!id,
  });

  const templatesQuery = useQuery({
    queryKey: ["templates", id, { status: templateStatus }],
    queryFn: () =>
      listTemplates({
        campaign_id: id!,
        ...(templateStatus ? { status: templateStatus } : {}),
        limit: 100,
      }),
    enabled: !!id,
  });

  const updateCampaignMut = useMutation({
    mutationFn: (patch: CampaignUpdate) => updateCampaign(id!, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["campaign", id] });
      qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
  });

  const generateMut = useMutation({
    mutationFn: (vpc: number) => triggerCampaignGenerate(id!, vpc),
    onSuccess: (res) => {
      setGenerateResult(res);
      qc.invalidateQueries({ queryKey: ["templates", id] });
      qc.invalidateQueries({ queryKey: ["campaign", id] });
    },
  });

  const sendMut = useMutation({
    mutationFn: (emails: string[]) =>
      triggerCampaignSend(id!, { email_list: emails }),
    onSuccess: (res) => {
      setSendResult(res);
    },
  });

  const updateTemplateMut = useMutation({
    mutationFn: ({
      templateId,
      patch,
    }: {
      templateId: string;
      patch: TemplateUpdate;
    }) => updateTemplate(templateId, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["templates", id] });
    },
  });

  if (!id) return <div>Missing campaign id</div>;
  if (campaignQuery.isLoading) return <div className="text-sm font-mono text-muted-foreground">Loading...</div>;
  if (campaignQuery.error) {
    return (
      <div className="text-sm font-mono text-danger">
        {(campaignQuery.error as Error).message}
      </div>
    );
  }
  const campaign = campaignQuery.data;
  if (!campaign) return <div>Not found</div>;

  const status = campaign.status as CampaignStatus;
  const canActivate = status !== "active" && status !== "archived";
  const canPause = status === "active";
  const canArchive = status !== "archived";

  const setStatus = (s: CampaignStatus) => updateCampaignMut.mutate({ status: s });

  const templates = templatesQuery.data?.templates ?? [];

  return (
    <div className="space-y-6">
      {/* Masthead */}
      <div className="border-b border-border pb-4">
        <Link to="/campaigns" className="text-xs font-mono text-muted-foreground hover:text-foreground uppercase tracking-[0.1em]">
          &larr; Campaigns
        </Link>
        <div className="mt-2 flex items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{campaign.name}</h1>
          <Badge variant={statusVariant(campaign.status)}>{campaign.status}</Badge>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {canActivate && (
          <Button
            size="sm"
            onClick={() => setStatus("active")}
            disabled={updateCampaignMut.isPending}
          >
            Activate
          </Button>
        )}
        {canPause && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setStatus("paused")}
            disabled={updateCampaignMut.isPending}
          >
            Pause
          </Button>
        )}
        {canArchive && (
          <Button
            size="sm"
            variant="destructive"
            onClick={() => setStatus("archived")}
            disabled={updateCampaignMut.isPending}
          >
            Archive
          </Button>
        )}
        {updateCampaignMut.error && (
          <span className="text-sm font-mono text-danger">
            {(updateCampaignMut.error as Error).message}
          </span>
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Brief</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div>
            <span className="text-xs font-mono text-muted-foreground uppercase tracking-[0.1em]">Vertical </span>
            <span className="font-mono">{campaign.vertical}</span>
          </div>
          <div>
            <span className="text-xs font-mono text-muted-foreground uppercase tracking-[0.1em]">Created </span>
            <span className="font-mono tabular-nums">{formatDate(campaign.created_at)}</span>
          </div>
          <div>
            <div className="text-xs font-mono text-muted-foreground uppercase tracking-[0.1em] mb-1">AI Brief</div>
            <div className="whitespace-pre-wrap rounded border border-border bg-surface-2 p-3 text-sm font-mono">
              {campaign.ai_brief || "—"}
            </div>
          </div>
          <div>
            <div className="text-xs font-mono text-muted-foreground uppercase tracking-[0.1em] mb-1">MAB Config</div>
            <pre className="overflow-auto rounded border border-border bg-surface-2 p-3 text-xs font-mono">
              {prettyJson(campaign.mab_config)}
            </pre>
          </div>
          <div>
            <div className="text-xs font-mono text-muted-foreground uppercase tracking-[0.1em] mb-1">Cohort Filter</div>
            <pre className="overflow-auto rounded border border-border bg-surface-2 p-3 text-xs font-mono">
              {prettyJson(campaign.cohort_filter)}
            </pre>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Generate Variants</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-end gap-2">
            <div className="space-y-1">
              <label className="text-xs font-mono uppercase tracking-[0.1em] text-muted-foreground">Variants per cohort</label>
              <Input
                type="number"
                min={1}
                max={20}
                className="w-32"
                value={variantsPerCohort}
                onChange={(e) =>
                  setVariantsPerCohort(
                    Math.max(1, parseInt(e.target.value || "1", 10)),
                  )
                }
              />
            </div>
            <Button
              onClick={() => generateMut.mutate(variantsPerCohort)}
              disabled={generateMut.isPending}
            >
              {generateMut.isPending ? "Generating..." : "Generate Variants"}
            </Button>
          </div>
          {generateMut.error && (
            <div className="text-sm font-mono text-danger">
              {(generateMut.error as Error).message}
            </div>
          )}
          {generateResult && (
            <div className="space-y-1 rounded border border-border bg-surface-2 p-3 text-sm font-mono">
              <div className="tabular-nums">Cohorts processed: {generateResult.cohorts_processed}</div>
              <div className="tabular-nums">Templates created: {generateResult.templates_created}</div>
              <div className="tabular-nums">Variants total: {generateResult.variants_total}</div>
              {generateResult.errors.length > 0 && (
                <div className="text-danger">
                  <div className="font-medium">Errors:</div>
                  <ul className="list-disc pl-5">
                    {generateResult.errors.map((err, i) => (
                      <li key={i}>{err}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Templates</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <label className="text-xs font-mono uppercase tracking-[0.1em] text-muted-foreground">Status</label>
            <Select
              options={TEMPLATE_STATUS_OPTIONS}
              value={templateStatus}
              onChange={setTemplateStatus}
            />
          </div>

          {templatesQuery.isLoading && <div className="text-sm font-mono text-muted-foreground">Loading templates...</div>}
          {templatesQuery.error && (
            <div className="text-sm font-mono text-danger">
              {(templatesQuery.error as Error).message}
            </div>
          )}

          {!templatesQuery.isLoading && !templatesQuery.error && (
            <Table>
              <Thead>
                <Tr>
                  <Th>Cohort</Th>
                  <Th>Variant</Th>
                  <Th>Status</Th>
                  <Th>Subject</Th>
                  <Th>Created</Th>
                  <Th>Actions</Th>
                </Tr>
              </Thead>
              <Tbody>
                {templates.length === 0 && (
                  <Tr>
                    <Td
                      colSpan={6}
                      className="text-center font-mono text-xs text-muted-foreground"
                    >
                      No templates
                    </Td>
                  </Tr>
                )}
                {templates.map((t) => {
                  const act = (s: TemplateStatus) =>
                    updateTemplateMut.mutate({
                      templateId: t.id,
                      patch: { status: s },
                    });
                  return (
                    <Tr key={t.id}>
                      <Td className="font-mono text-xs tabular-nums">{t.cohort_key}</Td>
                      <Td className="font-mono text-xs">{t.variant_label}</Td>
                      <Td>
                        <Badge variant={statusVariant(t.status)}>
                          {t.status}
                        </Badge>
                      </Td>
                      <Td className="text-sm" title={t.subject}>{truncate(t.subject, 50)}</Td>
                      <Td className="font-mono text-xs tabular-nums">{formatDate(t.created_at)}</Td>
                      <Td>
                        <div className="flex flex-wrap gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => {
                              setPreviewTemplate(t);
                              setPreviewTab("html");
                            }}
                          >
                            Preview
                          </Button>
                          {t.status !== "approved" && (
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => act("approved")}
                              disabled={updateTemplateMut.isPending}
                            >
                              Approve
                            </Button>
                          )}
                          {t.status !== "rejected" && (
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => act("rejected")}
                              disabled={updateTemplateMut.isPending}
                            >
                              Reject
                            </Button>
                          )}
                          {t.status !== "retired" && (
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => act("retired")}
                              disabled={updateTemplateMut.isPending}
                            >
                              Retire
                            </Button>
                          )}
                        </div>
                      </Td>
                    </Tr>
                  );
                })}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Send to Email List</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            placeholder="Paste emails (comma or newline separated)"
            value={emailsRaw}
            onChange={(e) => setEmailsRaw(e.target.value)}
            className="min-h-[120px]"
          />
          <div className="flex items-center gap-2">
            <Button
              onClick={() => {
                const emails = parseEmails(emailsRaw);
                if (emails.length === 0) return;
                sendMut.mutate(emails);
              }}
              disabled={sendMut.isPending || parseEmails(emailsRaw).length === 0}
            >
              {sendMut.isPending ? "Sending..." : "Send"}
            </Button>
            <span className="text-xs font-mono text-muted-foreground">
              {parseEmails(emailsRaw).length} recipient(s)
            </span>
          </div>
          {sendMut.error && (
            <div className="text-sm font-mono text-danger">
              {(sendMut.error as Error).message}
            </div>
          )}
          {sendResult && (
            <div className="space-y-1 rounded border border-border bg-surface-2 p-3 text-sm font-mono tabular-nums">
              <div>Queued: {sendResult.queued}</div>
              <div>Skipped (no template): {sendResult.skipped_no_template}</div>
              <div>Skipped (not found): {sendResult.skipped_not_found}</div>
            </div>
          )}
        </CardContent>
      </Card>

      {previewTemplate && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setPreviewTemplate(null)}
        >
          <div
            className="flex h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg border border-border bg-surface-1 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-4 border-b border-border p-4">
              <div className="min-w-0 space-y-1">
                <div className="text-xs font-mono uppercase tracking-[0.1em] text-muted-foreground">
                  {previewTemplate.cohort_key} / {previewTemplate.variant_label}
                </div>
                <div className="truncate text-sm font-semibold" title={previewTemplate.subject}>
                  {previewTemplate.subject}
                </div>
                {previewTemplate.preheader && (
                  <div
                    className="truncate text-xs font-mono text-muted-foreground"
                    title={previewTemplate.preheader}
                  >
                    {previewTemplate.preheader}
                  </div>
                )}
              </div>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setPreviewTemplate(null)}
              >
                Close
              </Button>
            </div>
            <div className="flex gap-1 border-b border-border px-4 pt-2">
              <button
                type="button"
                onClick={() => setPreviewTab("html")}
                className={`px-3 py-2 text-xs font-mono uppercase tracking-[0.1em] border-b-2 ${
                  previewTab === "html"
                    ? "border-foreground text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                HTML
              </button>
              <button
                type="button"
                onClick={() => setPreviewTab("text")}
                className={`px-3 py-2 text-xs font-mono uppercase tracking-[0.1em] border-b-2 ${
                  previewTab === "text"
                    ? "border-foreground text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                Plain text
              </button>
            </div>
            <div className="border-b border-border px-4 py-1 text-xs font-mono text-muted-foreground tabular-nums">
              html: {(previewTemplate.body_html ?? "").length} chars · text:{" "}
              {(previewTemplate.body_text ?? "").length} chars
            </div>
            <div className="flex-1 overflow-hidden bg-surface-2">
              {previewTab === "html" ? (
                previewTemplate.body_html ? (
                  <iframe
                    title="template-preview"
                    sandbox=""
                    srcDoc={previewTemplate.body_html}
                    className="h-full w-full border-0 bg-white"
                  />
                ) : (
                  <div className="flex h-full items-center justify-center text-sm font-mono text-muted-foreground">
                    (body_html is empty)
                  </div>
                )
              ) : previewTemplate.body_text ? (
                <pre className="h-full overflow-auto whitespace-pre-wrap p-4 text-sm font-mono">
                  {previewTemplate.body_text}
                </pre>
              ) : (
                <div className="flex h-full items-center justify-center text-sm font-mono text-muted-foreground">
                  (body_text is empty)
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
