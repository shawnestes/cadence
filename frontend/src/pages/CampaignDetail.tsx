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
  return s.length > n ? `${s.slice(0, n)}…` : s;
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
  if (campaignQuery.isLoading) return <div>Loading...</div>;
  if (campaignQuery.error) {
    return (
      <div className="text-destructive">
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
      <div className="flex items-center gap-3">
        <Link to="/campaigns" className="text-sm text-muted-foreground hover:underline">
          ← Campaigns
        </Link>
      </div>
      <div className="flex items-center gap-3">
        <h2 className="text-2xl font-semibold">{campaign.name}</h2>
        <Badge variant={statusVariant(campaign.status)}>{campaign.status}</Badge>
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
          <span className="text-sm text-destructive">
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
            <span className="text-muted-foreground">Vertical: </span>
            {campaign.vertical}
          </div>
          <div>
            <span className="text-muted-foreground">Created: </span>
            {formatDate(campaign.created_at)}
          </div>
          <div>
            <div className="text-muted-foreground">AI brief</div>
            <div className="whitespace-pre-wrap rounded-md bg-muted/40 p-3">
              {campaign.ai_brief || "—"}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">MAB config</div>
            <pre className="overflow-auto rounded-md bg-muted/40 p-3 text-xs">
              {prettyJson(campaign.mab_config)}
            </pre>
          </div>
          <div>
            <div className="text-muted-foreground">Cohort filter</div>
            <pre className="overflow-auto rounded-md bg-muted/40 p-3 text-xs">
              {prettyJson(campaign.cohort_filter)}
            </pre>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Generate variants</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-end gap-2">
            <div className="space-y-1">
              <label className="text-sm font-medium">Variants per cohort</label>
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
              {generateMut.isPending ? "Generating..." : "Generate variants"}
            </Button>
          </div>
          {generateMut.error && (
            <div className="text-sm text-destructive">
              {(generateMut.error as Error).message}
            </div>
          )}
          {generateResult && (
            <div className="space-y-1 rounded-md bg-muted/40 p-3 text-sm">
              <div>Cohorts processed: {generateResult.cohorts_processed}</div>
              <div>Templates created: {generateResult.templates_created}</div>
              <div>Variants total: {generateResult.variants_total}</div>
              {generateResult.errors.length > 0 && (
                <div className="text-destructive">
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
            <label className="text-sm text-muted-foreground">Status</label>
            <Select
              options={TEMPLATE_STATUS_OPTIONS}
              value={templateStatus}
              onChange={setTemplateStatus}
            />
          </div>

          {templatesQuery.isLoading && <div>Loading templates...</div>}
          {templatesQuery.error && (
            <div className="text-destructive">
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
                      className="text-center text-muted-foreground"
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
                      <Td className="font-mono text-xs">{t.cohort_key}</Td>
                      <Td>{t.variant_label}</Td>
                      <Td>
                        <Badge variant={statusVariant(t.status)}>
                          {t.status}
                        </Badge>
                      </Td>
                      <Td title={t.subject}>{truncate(t.subject, 50)}</Td>
                      <Td>{formatDate(t.created_at)}</Td>
                      <Td>
                        <div className="flex flex-wrap gap-1">
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
          <CardTitle>Send to email list</CardTitle>
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
            <span className="text-sm text-muted-foreground">
              {parseEmails(emailsRaw).length} recipient(s)
            </span>
          </div>
          {sendMut.error && (
            <div className="text-sm text-destructive">
              {(sendMut.error as Error).message}
            </div>
          )}
          {sendResult && (
            <div className="space-y-1 rounded-md bg-muted/40 p-3 text-sm">
              <div>Queued: {sendResult.queued}</div>
              <div>Skipped (no template): {sendResult.skipped_no_template}</div>
              <div>Skipped (not found): {sendResult.skipped_not_found}</div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
