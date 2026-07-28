# SPEC Addendum 003 — Multiple ESPs (Sending Identities)

**Status**: Draft — awaiting approval. Extends SPEC §5, §8, §12, §13, §14. Amends §19: "Multi-ESP failover or routing" moves from non-goal to "routing now, failover later".
**Date**: 2026-07-23
**Reason**: v1 is SES-only with one global sending config in Settings. We want to send through other providers (SendGrid, ExpertSender, …) per campaign — and eventually multiple SES accounts — without touching the send pipeline each time.

---

## 1. Concept: sending identity

A **sending identity** = (ESP provider, credentials, domain, from-address, provider-specific config). Every send is dispatched *as* exactly one identity. The existing `ESPAdapter` interface is already provider-neutral (`send`, `get_reputation`, `parse_webhook`, `add_to_esp_suppression`; `sends.esp` + `sends.esp_message_id` are generic) — this addendum adds the *routing*, not a new abstraction.

Compliance stays **global per recipient** regardless of provider: suppression, frequency caps, quiet hours, engagement tiers. Providers isolate deliverability, never caps.

## 2. Data model

### 2.1 New table

```sql
CREATE TABLE sending_identities (
  id               uuid PRIMARY KEY,           -- uuid7
  tenant           text NOT NULL,
  name             text NOT NULL,              -- "otto-ses-primary", "otto-sendgrid"
  esp_provider     text NOT NULL,              -- 'ses' | 'ses_smtp' | 'sendgrid' | 'expertsender' | 'noop'
  from_address     text NOT NULL,
  sending_domain   text NOT NULL,
  credentials_ref  text,                       -- env-var prefix or Secrets Manager name; never raw secrets
  provider_config  jsonb NOT NULL DEFAULT '{}',-- SES: {region, configuration_set, managed_pool_name, role_arn, account_key}
                                               -- SendGrid: {ip_pool, ...}
  daily_quota      int NOT NULL DEFAULT 50000,
  status           text NOT NULL DEFAULT 'active',   -- active|paused
  is_default       boolean NOT NULL DEFAULT false,   -- one default per tenant
  created_at       timestamptz NOT NULL,
  updated_at       timestamptz NOT NULL,
  UNIQUE (tenant, name)
);
```

### 2.2 Column additions

- `campaigns.sending_identity_id uuid NULL` → FK; NULL = tenant default.
- `sends.sending_identity_id uuid NOT NULL` → stamped at queue time (alongside the existing `esp` name), drives per-identity stats and quota.

### 2.3 Migration/backfill

One migration: create table, seed a `default` SES identity per tenant from current Settings, backfill `sends.sending_identity_id`, add NOT NULL. Settings remain only as the seed for that default.

## 3. Adapter layer

- Factory becomes `get_esp_adapter(identity) -> ESPAdapter`, cached per identity id, switching on `esp_provider`. `CADENCE_ESP_BACKEND=noop` still forces `NoopESP` everywhere (dev/tests unchanged).
- **v1 of this addendum ships routing + the SES implementation only.** Each new provider later = one new adapter class implementing the 4 methods + a webhook route; zero pipeline changes. Adapter capability gaps are legal: `get_reputation` may return an empty snapshot, `add_to_esp_suppression` may be a no-op, per provider.
- Fix the existing leak: `app/api/stats.py` SES-account call routes through the adapter.

## 4. Send path

- Queue time: resolve identity (campaign's or tenant default), stamp `sends.sending_identity_id` + `sends.esp`. Paused identity → sends stay queued (alert, don't fail).
- Dispatch time: send worker groups the claimed batch by identity and dispatches each group through its adapter; `from_address` comes from the identity (global fallback removed).
- Unsub links: identity-stamped sends build unsubscribe URLs on `https://unsub.<sending_domain>` (override per identity via `provider_config.unsub_base_url`); legacy NULL-identity sends keep the global `CADENCE_UNSUB_BASE_URL`. Tokens are host-independent — the shared unsub endpoint verifies them regardless of which host served the link.

## 5. Quota + rate limiting

- Daily quota per identity (`daily_quota`). For SES identities sharing an AWS account, `provider_config.account_key` groups them so the account cap is shared. Per-second rate limit stays one global bucket v1.

## 6. Webhooks

- One route per provider: `/webhooks/ses` (exists), `/webhooks/sendgrid`, `/webhooks/expertsender` added with their adapters. Each route: verify provider signature → `adapter.parse_webhook` → the existing shared event-ingestion path (join on `esp_message_id`, which is already provider-generic).

## 7. Reputation / stats / dashboard

- Reputation monitor iterates active identities; providers without metrics contribute nothing (no alerts, no crash).
- Stats endpoints gain optional `identity` filter; dashboard groups by sending domain/provider with a health strip per identity. Per-ISP CloudWatch tiles remain SES-only (marked as such in UI).

## 8. API + UI

- CRUD: `GET/POST/PATCH /api/sending-identities` (pause, don't delete). Campaign create/update accepts `sending_identity_id`.
- UI second pass: identity picker on campaign form, provider/domain column on campaign list, dashboard grouping.

## 9. Explicitly deferred

- Additional adapter implementations (SendGrid/ExpertSender classes) — separate follow-ups per provider, trivial once routing exists
- Multiple SES accounts (schema supports it via `provider_config`; AssumeRole plumbing lands with the second SES identity)
- Automatic failover between providers (routing is deterministic per campaign in v1)
- Automated DNS/DKIM provisioning for new domains

## 10. SPEC sections to update on merge

- §5: `sending_identities` table + new columns
- §8.1/§8.2: per-identity factory; provider capability-gap rules
- §12: Settings = seed for default identity only
- §13: quota per identity, account-grouped for SES
- §14: per-identity alert conditions
- §19: amend the multi-ESP non-goal
