export type ApiError = { status: number; detail: string };

export type Vertical = "auto" | "home";

export type RecipientStatus =
  | "active"
  | "suppressed"
  | "bounced_hard"
  | "complained";

export type SuppressionReason =
  | "unsubscribe"
  | "hard_bounce"
  | "complaint"
  | "manual"
  | "otto_push";

export type RecipientIngestIn = {
  email: string;
  tenant?: string;
  external_id: string | null;
  vertical: Vertical;
  consent_source: string;
  consent_at: string;
  attributes?: Record<string, unknown>;
};

export type RecipientIngestOut = {
  id: string;
  tenant: string;
  email: string;
  status: RecipientStatus;
  created: boolean;
};

export type SuppressionIn = {
  email: string;
  tenant?: string;
  reason: SuppressionReason;
  source: string;
  campaign_id: string | null;
  notes: string | null;
};

export type SuppressionOut = {
  id: string;
  tenant: string;
  email: string;
  reason: SuppressionReason;
  source: string;
  campaign_id: string | null;
  notes: string | null;
  suppressed_at: string;
  created_at: string;
  updated_at: string;
};

export type SuppressionListOut = {
  suppressions: SuppressionOut[];
  total: number;
};

// ------------------ Stats ------------------
export type StatsWindow = "24h" | "7d" | "30d";
export type StatsBucket = "1h" | "1d";

export type Topline = {
  sends: number;
  delivered: number;
  bounces: number;
  hard_bounces: number;
  soft_bounces: number;
  complaints: number;
  unique_opens: number;
  unique_clicks: number;
  unsubscribes: number;
  delivery_rate: number;
  bounce_rate: number;
  complaint_rate: number;
  open_rate: number;
  click_rate: number;
  unsubscribe_rate: number;
};

export type ToplineWindow = {
  window: string;
  topline: Topline;
};

export type TimeseriesPoint = {
  bucket_start: string;
  sends: number;
  delivered: number;
  bounces: number;
  complaints: number;
  opens: number;
  clicks: number;
};

export type TimeseriesOut = {
  points: TimeseriesPoint[];
  bucket: string;
};

export type ToplineStreamEvent = {
  type: "topline";
  data: Topline;
  ts: string;
  window: string;
};

export type CampaignVolumeRow = {
  campaign_id: string;
  campaign_name: string;
  sends: number;
  delivered: number;
  bounces: number;
  complaints: number;
  delivery_rate: number;
  bounce_rate: number;
  complaint_rate: number;
};

export type CampaignsVolumeOut = {
  window: string;
  campaigns: CampaignVolumeRow[];
};

export type SESReputationOut = {
  tenant: string;
  sending_enabled: boolean;
  production_access_enabled: boolean;
  enforcement_status: string | null;
  max_24_hour_send: number;
  sent_last_24_hours: number;
  max_send_rate: number;
  reputation_sending_enabled: boolean | null;
  fetched_at: string;
};

// ------------------ ISP Reputation ------------------
export type IspName = "Gmail" | "Yahoo" | "Outlook" | "Apple" | "Other";

export type IspReputationRow = {
  isp: IspName;
  reputation: number | null;
  delivery_rate: number | null;
  sent: number;
  delivered: number;
  effective_daily_rate: number | null;
};

export type IspReputationOut = {
  sending_allowed: boolean | null;
  dedicated_ip_count: number | null;
  isps: IspReputationRow[];
};

// ------------------ Recipients ------------------
export type RecipientSummary = {
  id: string;
  email: string;
  tenant: string;
  vertical: string;
  status: string;
  external_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  created_at: string;
  updated_at: string;
  attributes: Record<string, unknown>;
};

export type JourneySummary = {
  id: string;
  workflow_id: string;
  workflow_version: number;
  status: string;
  current_node_id: string;
  entered_at: string;
  completed_at: string | null;
};

export type SendSummary = {
  id: string;
  campaign_id: string;
  template_id: string;
  variant_label: string;
  status: string;
  queued_at: string;
  sent_at: string | null;
  delivered_at: string | null;
  failure_reason: string | null;
};

export type EventSummary = {
  id: string;
  event_type: string;
  event_subtype: string | null;
  url: string | null;
  occurred_at: string;
};

export type RecipientDetail = {
  recipient: RecipientSummary;
  journeys: JourneySummary[];
  sends: SendSummary[];
  events: EventSummary[];
};

export type RecipientSearchOut = {
  recipients: RecipientSummary[];
  total: number;
};

// ------------------ Campaigns ------------------
export type CampaignStatus =
  | "draft"
  | "generating"
  | "ready"
  | "active"
  | "paused"
  | "archived";

export type Campaign = {
  id: string;
  tenant: string;
  workflow_id: string | null;
  node_id: string | null;
  name: string;
  vertical: string;
  status: string;
  cohort_filter: Record<string, unknown> | null;
  ai_brief: string | null;
  mab_config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type CampaignOut = Campaign;

export type CampaignIn = {
  name: string;
  tenant?: string;
  vertical: string;
  workflow_id?: string | null;
  node_id?: string | null;
  cohort_filter?: Record<string, unknown> | null;
  ai_brief?: string | null;
  mab_config?: Record<string, unknown>;
};

export type CampaignUpdate = {
  name?: string;
  status?: CampaignStatus;
  cohort_filter?: Record<string, unknown> | null;
  ai_brief?: string | null;
  mab_config?: Record<string, unknown>;
};

export type CampaignListOut = {
  campaigns: CampaignOut[];
  total: number;
};

export type CampaignSendRequest = {
  recipient_ids?: string[];
  email_list?: string[];
  tenant?: string;
};

export type CampaignSendResult = {
  queued: number;
  skipped_no_template: number;
  skipped_not_found: number;
};

export type CampaignGenerateResult = {
  cohorts_processed: number;
  templates_created: number;
  variants_total: number;
  errors: string[];
};

// ------------------ Templates ------------------
export type TemplateStatus = "generated" | "approved" | "rejected" | "retired";

export type Template = {
  id: string;
  tenant: string;
  campaign_id: string;
  cohort_key: string;
  variant_label: string;
  subject: string;
  preheader: string | null;
  body_html: string;
  body_text: string;
  ai_meta: Record<string, unknown> | null;
  status: string;
  created_at: string;
  updated_at: string;
};

export type TemplateOut = Template;

export type TemplateIn = {
  campaign_id: string;
  tenant?: string;
  cohort_key: string;
  variant_label: string;
  subject: string;
  preheader?: string | null;
  body_html: string;
  body_text: string;
  ai_meta?: Record<string, unknown> | null;
};

export type TemplateUpdate = {
  status?: TemplateStatus;
  subject?: string;
  preheader?: string | null;
  body_html?: string;
  body_text?: string;
};

export type TemplateListOut = {
  templates: TemplateOut[];
  total: number;
};

// ------------------ Alerts ------------------
export type AlertStatus = "pending" | "active" | "resolved" | "all";
export type AlertSeverity = "warn" | "critical";

export type AlertOut = {
  id: string;
  tenant: string;
  metric: string;
  severity: AlertSeverity;
  threshold: number;
  value: number;
  status: AlertStatus;
  pending_since: string;
  activated_at: string | null;
  resolved_at: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type AlertsListOut = {
  alerts: AlertOut[];
  total: number;
};

// ------------------ Workflows ------------------
export type WorkflowStatus = "draft" | "active" | "paused" | "archived";

export type Workflow = {
  id: string;
  tenant: string;
  name: string;
  vertical: string;
  status: string;
  definition: Record<string, unknown>;
  entry_segment: Record<string, unknown> | null;
  version: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkflowOut = Workflow;

export type WorkflowIn = {
  name: string;
  tenant?: string;
  vertical: string;
  definition: Record<string, unknown>;
  entry_segment?: Record<string, unknown> | null;
  created_by?: string | null;
};

export type WorkflowUpdate = {
  name?: string;
  status?: WorkflowStatus;
  definition?: Record<string, unknown>;
  entry_segment?: Record<string, unknown> | null;
};

export type WorkflowListOut = {
  workflows: WorkflowOut[];
  total: number;
};
