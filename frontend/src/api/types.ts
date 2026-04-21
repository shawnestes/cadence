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
