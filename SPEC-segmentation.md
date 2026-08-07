# SPEC: Contact Segmentation Engine

Status: draft (rev 3 — implementation decisions applied)
Owner: Shawn
Stack: Python / FastAPI / APScheduler, Aurora Postgres, AWS

## 0. Implementation decisions (M-SEG-0)

Binding overrides. Where these conflict with later prose, these win — the prose is amended by reference and should be read through this lens.

- **D1. IDs are UUID v7.** `contact_id` and `campaign_id` throughout the new tables are UUID v7 (not `bigint`), FK to `recipients.id` and `campaigns.id`. `segment_definition.segment_id` is UUID v7 (not `bigserial`). All `bigint` PK/FK types in the DDL below are amended to UUID.
- **D2. `event_type` stays smallint** per DDL. A code-lookup table (`event_type_code`) maps SES event names to smallint codes; existing text-based workers (`reputation_monitor`, `stats_rollup`) get a translation shim rather than being rewritten.
- **D3. No consent backfill.** Existing recipients without a `consent_proof` row land in `contact_quarantine` on day 1. The project is not live (no production sends yet); day-1 quarantine has no cost. `consent_proof_id NOT NULL` is enforced from the first migration.
- **D4. Blast-radius guardrail is inline in the nightly pass.** Vigil is not integrated in this project. §4/§5 language about Vigil ownership of the guardrail is superseded: the nightly pass reads its own previous `segment_tier_snapshot`, computes the delta, and aborts before commit on breach.
- **D5. Event ingest runs parallel-write during rollout.** New SQS poller writes `contact_event`; existing HTTPS webhook keeps writing to `events`. The `events` table becomes read-only once the poller is validated. No data migration between the two.
- **D6. AWS SES→SNS→SQS infra is a M-SEG-2 deliverable.** Config Set, SNS topic, SQS queue, and IAM do not exist yet; provisioning (Terraform or manual) is scoped into M-SEG-2 and gates the poller.
- **D7. `aiodns` is an approved new dependency** for async MX resolution in domain reclassification.
- **D8. Google Postmaster Tools integration ships as a no-op stub.** M-SEG-6 includes the code path; it is inert when `gpt_api_credentials_json` is unset. Missing data is logged as `gpt_data_unavailable`, not treated as passing.
- **D9. No send-path shadow mode.** Project is not live — M-SEG-7 wires `send_worker` directly to `segment_member_current` without a dual-query validation window.
- **D10. Per-provider engagement overrides ship in v1.** `engagement.yaml` includes `overrides.by_mailbox_provider.cable.cold_min_sends_lifetime: 8` from the initial commit.
- **D11. Multi-tenancy.** All new tables carry `tenant text NOT NULL`. `segment_member_current` filters by tenant on every join. The DDL below omits this column for brevity — read every table as having it.
- **D12. `contact_event` replaces `events` for new SES ingest** once the poller is confirmed (D5). Existing `events` data is not migrated; it remains queryable for historical stats until the 13-month partition drop takes it.

Rev 3 overrides earlier "Resolved" tags in §13 where in conflict. OQ2 (scanner window), OQ5 (unwrapped-link rate), OQ6 (attribution window), OQ7 (Gmail automated response), OQ12 (click_registry retention), OQ13 (List-Unsubscribe status) remain open.

## 1. Scope

Precomputed per-contact profile table. Segments are compiled predicates evaluated against the profile only — never against raw events.

| Parameter | Value |
|---|---|
| Contacts (email) | ~1M |
| Contacts (SMS, phase 2) | ~1M, shared `contact_id` |
| Events | ~4M/mo email |
| Segments | 100s |
| Authoring | Engineer, YAML in repo |
| Freshness SLA | 2 min incremental, nightly full |
| Send-time eval | None required |
| Event sources | SES (SNS→SQS); origin platform (clicks + conversions) |
| Consumption | In-place read of `segment_member_current`. No export step. |

Out of scope: send orchestration, throttling, template rendering, marketer-facing UI.

## 2. Data model

Three layers. Segment predicates read layer 3 exclusively.

```sql
-- L1: append-only, monthly partitions, BRIN on occurred_at
CREATE TABLE contact_event (
  event_id        bigserial,
  contact_id      bigint      NOT NULL,
  channel         smallint    NOT NULL,   -- 1=email 2=sms
  event_type      smallint    NOT NULL,
  occurred_at     timestamptz NOT NULL,
  campaign_id     bigint,
  source          smallint    NOT NULL,   -- 1=ses 2=origin_platform
  provider_msg_id text,
  click_id        text,                   -- canonical click identity, see §6
  dedup_of        bigint,                 -- set when collapsed into a prior event
  classification  smallint,               -- 0=human 1=mpp 2=scanner, see §6
  meta            jsonb,
  PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE INDEX ON contact_event USING brin (occurred_at);
CREATE UNIQUE INDEX ON contact_event (source, provider_msg_id, event_type, occurred_at);
CREATE INDEX ON contact_event (click_id, event_type) WHERE click_id IS NOT NULL;

-- Flat, unpartitioned. Conversions lag clicks by days-to-weeks, so attribution
-- lookups would otherwise cross contact_event partitions on every conversion.
-- One row per click; a few million rows at 13mo retention.
CREATE TABLE click_registry (
  click_id       text PRIMARY KEY,
  contact_id     bigint      NOT NULL,
  channel        smallint    NOT NULL,
  campaign_id    bigint,
  clicked_at     timestamptz NOT NULL,
  classification smallint    NOT NULL,   -- 0=human 1=scanner; mutable, see §6
  reclassified_at timestamptz,
  converted_at   timestamptz,            -- set on first attributed conversion
  convert_count  int NOT NULL DEFAULT 0
);
CREATE INDEX ON click_registry (contact_id, clicked_at);
CREATE INDEX ON click_registry (classification, converted_at)
  WHERE classification = 1;              -- scanner false-positive measurement, §6

-- L2: daily rollup. Makes window decay incremental instead of a scan.
CREATE TABLE contact_activity_daily (
  contact_id      bigint   NOT NULL,
  channel         smallint NOT NULL,
  day             date     NOT NULL,
  sends           int NOT NULL DEFAULT 0,
  delivers        int NOT NULL DEFAULT 0,
  opens           int NOT NULL DEFAULT 0,   -- human only
  opens_mpp       int NOT NULL DEFAULT 0,   -- machine prefetch, excluded everywhere
  clicks          int NOT NULL DEFAULT 0,   -- confirmed only, see §6
  clicks_bot      int NOT NULL DEFAULT 0,   -- scanner, excluded everywhere
  converts        int NOT NULL DEFAULT 0,
  bounces         int NOT NULL DEFAULT 0,
  complaints      int NOT NULL DEFAULT 0,
  PRIMARY KEY (contact_id, channel, day)
);

-- L3: one row per contact per channel. THE segment surface.
CREATE TABLE contact_profile (
  contact_id       bigint   NOT NULL,
  channel          smallint NOT NULL,

  email_domain     text,
  mailbox_provider smallint,            -- see §3
  reputation_group smallint,            -- receiving MX operator, see §3
  country          char(2),
  tz               text,

  -- recency. Human/confirmed events only. Self-decaying under `within`.
  first_send_at    timestamptz,
  last_send_at     timestamptz,
  last_open_at     timestamptz,
  last_click_at    timestamptz,
  last_convert_at  timestamptz,

  sends_30d int, opens_30d int, clicks_30d int,
  sends_90d int, opens_90d int, clicks_90d int, converts_90d int,
  bounces_90d int, complaints_90d int,

  sends_lifetime int, opens_lifetime int, clicks_lifetime int,

  engagement_score smallint,            -- 0..100
  engagement_tier  smallint,            -- see §4

  consent_state    smallint NOT NULL,   -- 1=opted_in 2=opted_out. 0 is not insertable, see §9
  consent_at       timestamptz,
  consent_proof_id bigint NOT NULL REFERENCES consent_proof,
  suppressed_at    timestamptz,
  suppress_reason  smallint,
  sendable         boolean GENERATED ALWAYS AS
                     (consent_state = 1 AND suppressed_at IS NULL) STORED,

  profile_version  bigint NOT NULL,
  tier_config_version text NOT NULL,    -- hash of engagement.yaml; mismatch => rebuild, see §4
  windows_valid_to date,
  updated_at       timestamptz NOT NULL,
  PRIMARY KEY (contact_id, channel)
);

CREATE INDEX ON contact_profile (channel, mailbox_provider, last_open_at) WHERE sendable;
CREATE INDEX ON contact_profile (channel, reputation_group, last_open_at) WHERE sendable;
CREATE INDEX ON contact_profile (channel, engagement_tier) WHERE sendable;
CREATE INDEX ON contact_profile (profile_version);

CREATE TABLE consent_proof (
  consent_proof_id bigserial PRIMARY KEY,
  contact_id   bigint NOT NULL,
  captured_at  timestamptz NOT NULL,
  source_url   text,
  form_id      text,
  ip           inet,
  user_agent   text,
  disclosure_text_hash text NOT NULL,
  channels     smallint[] NOT NULL,
  raw          jsonb
);

CREATE TABLE segment_definition (
  segment_id bigserial PRIMARY KEY,
  key        text UNIQUE NOT NULL,
  name       text NOT NULL,
  channel    smallint NOT NULL,
  predicate  jsonb NOT NULL,
  spec_hash  text NOT NULL,       -- change => full rebuild of this segment
  active     boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL
);

CREATE TABLE segment_member (
  segment_id bigint NOT NULL,
  contact_id bigint NOT NULL,
  channel    smallint NOT NULL,
  added_at   timestamptz NOT NULL,
  removed_at timestamptz,
  PRIMARY KEY (segment_id, contact_id, added_at)
);
CREATE INDEX ON segment_member (segment_id) WHERE removed_at IS NULL;
```

Retention: drop `contact_event` partitions > 13 months. Roll `contact_activity_daily` to monthly grain beyond 90 days. Soft-removed `segment_member` rows purge at 90 days into a monthly churn aggregate.

## 3. Mailbox provider classification

Two independent axes. Conflating them is the main foot-gun.

- **`mailbox_provider`** — brand bucket. The business-facing axis ("all Yahoo").
- **`reputation_group`** — who operates the receiving MX. Governs deliverability, throttling, and complaint-rate thresholds.

They diverge. AT&T-family domains are telco brands on Yahoo-operated infrastructure; throttling them separately from Yahoo shares a reputation limit invisibly.

```sql
CREATE TABLE domain_provider (
  domain           text PRIMARY KEY,
  mailbox_provider smallint NOT NULL,
  reputation_group smallint NOT NULL,
  source           smallint NOT NULL,   -- 1=seed 2=mx_lookup 3=manual
  mx_host          text,
  checked_at       timestamptz
);
```

| `mailbox_provider` | Domains | `reputation_group` |
|---|---|---|
| `gmail` | gmail.com, googlemail.com | `gmail` |
| `microsoft` | hotmail.com, msn.com, outlook.com, live.com + ccTLD variants | `microsoft` |
| `yahoo` | yahoo.com + ccTLD variants, ymail.com, rocketmail.com, aol.com, aim.com | `yahoo` |
| `cable` | comcast.net, charter.net, spectrum.net, rr.com, twc.com, cox.net, optonline.net, mediacombb.net, windstream.net, frontier.com, earthlink.net, juno.com, netzero.net, att.net, sbcglobal.net, bellsouth.net, ameritech.net, pacbell.net, swbell.net, verizon.net | per-operator; **AT&T-family and verizon.net → `yahoo`** |
| `apple` | icloud.com, me.com, mac.com | `apple` |
| `corporate` | any domain whose MX is Google Workspace or M365 but isn't consumer freemail | `gmail` / `microsoft` |
| `other` | unmatched | `other` |

Decisions applied: single `cable` bucket (Q2). Corporate split on the brand axis only, MX truth preserved on the reputation axis (Q1).

Long tail: on first sight of an unknown domain, enqueue an MX lookup, cache in `domain_provider`, classify by MX host pattern (`*.google.com`, `*.protection.outlook.com`, `*.yahoodns.net`). Never resolve MX on the hot ingest path — unknown sits in `other` until resolved. That is correct behavior, not a gap.

Normalization on write: lowercase, trim, strip trailing dot. Do **not** strip Gmail dots/plus-tags here; that is identity dedup, a separate concern, and doing it in this path corrupts the deliverable address.

## 4. Engagement tier

Tier structure is fixed; every threshold is configuration. `cold` gates segment membership globally per §7, so its threshold is a system-wide dial, not a reporting preference — hardcoding any of these numbers guarantees a schema-adjacent code change the first time one needs to move.

Ladder is ordered, first match wins. `opens_mpp` and `clicks_bot` are excluded from every count and every `last_*_at` referenced below.

```
active   : clicks_long   >= active_min_clicks_long
           OR converts_long >= active_min_converts_long
engaged  : opens_short   >= engaged_min_opens_short
passive  : opens_long    >= passive_min_opens_long
dormant  : sends_long    >= dormant_min_sends_long AND opens_long = 0
cold     : last_open_at IS NULL
           AND sends_lifetime >= cold_min_sends_lifetime
           AND first_send_at <= now() - cold_min_tenure_days
new      : sends_lifetime <  new_max_sends_lifetime
unknown  : fallthrough
```

`cold` is **terminal** (§13 Q10): an excluded contact is never mailed, so `last_open_at` can never become non-null. The tenure guard exists because of that terminality — send count alone would permanently kill a contact who received five messages in their first week and happened not to open (vacation, promo tab, delayed batch delivery). Both conditions must hold.

At `cold_min_tenure_days: 30` the earliest possible cold classification lands exactly as a contact's first full `window_short_days` closes, which is coherent by construction. Note that send cadence, not the threshold, determines how fast the cold pool fills: at weekly cadence a contact reaches 5 sends around day 30 and both conditions land together, so the guard is inert. At daily cadence the send count is met in week one and the guard is the only thing holding classification back for three more weeks. Revisit this number if cadence changes materially.

```yaml
# config/engagement.yaml
engagement:
  window_short_days: 30
  window_long_days:  90

  thresholds:
    active_min_clicks_long:    1
    active_min_converts_long:  1
    engaged_min_opens_short:   1
    passive_min_opens_long:    1
    dormant_min_sends_long:    3
    cold_min_sends_lifetime:   5     # system-wide segment filter, see §7
    cold_min_tenure_days:      30    # both conditions required; cold is terminal
    new_max_sends_lifetime:    3

  score:
    weight_click:          0.50
    weight_open:           0.30
    weight_convert:        0.20
    halflife_click_days:   30
    halflife_open_days:    21
    halflife_convert_days: 90
    penalty_complaint:     0.0
    penalty_bounce:        0.5

  cold_exclusion:
    enabled: true                    # segments must declare acknowledges_cold to include

  # Optional. Cable/telco domains under-report opens relative to Gmail, so identical
  # thresholds are not identical evidence. Global default unless overridden.
  overrides:
    by_mailbox_provider:
      cable:
        cold_min_sends_lifetime: 8
```

```
score = 100 * (
    weight_click   * decay(last_click_at,   halflife_click_days)
  + weight_open    * decay(last_open_at,    halflife_open_days)
  + weight_convert * decay(last_convert_at, halflife_convert_days)
) * penalty
penalty = penalty_complaint if complaints_90d > 0
          else penalty_bounce if bounces_90d > 0
          else 1.0
decay(t, h) = 0 if t is null else 2 ** (-(now - t).days / h)
```

Clicks carry the weight deliberately — opens are a weak post-MPP signal, and no tier is reachable by opens alone. This is only safe because `clicks` means *confirmed* clicks per §6; weighting raw SES clicks at 0.50 would make security scanners the largest single contributor to the score.

**Config changes are rebuilds.** `tier_config_version` = hash of the resolved `engagement` block, stored per profile row. A mismatch invalidates `engagement_tier`, `engagement_score`, and — because predicates reference tier — every segment membership. Handled by the same nightly mechanism as `spec_hash` (§5). Thresholds are not hot-reloadable and must not be.

**Blast-radius guardrail.** A one-character edit to `cold_min_sends_lifetime` can drop a large share of the base out of every segment at once, and nothing downstream looks broken — sends just get quieter. Before applying a changed config the nightly pass computes the resulting tier-population delta against current state and **aborts** if any tier's population moves more than `max_tier_shift_pct` (default 10%) without an explicit `--force`. The delta is logged and Slacked either way.

## 5. Refresh

**Ingest (continuous).** SES Configuration Set → SNS topic → SQS queue → poller. SNS→SQS rather than raw HTTPS: retry, DLQ, and backpressure for free. Origin-platform signals land on a parallel FastAPI endpoint into the same queue. Worker:

1. Dedupe on `(source, provider_msg_id, event_type, occurred_at)`.
2. Classify per §6 (human / mpp / scanner; confirmed / unconfirmed click).
3. Insert `contact_event`.
4. Upsert `contact_activity_daily` counters into the correct column.
5. Bump `profile_version`.
6. Complaint or hard bounce → set `suppressed_at` **on every channel for that contact**, synchronously, never deferred to the batch (Q5).

SES event types → `event_type`: Send, Delivery, Bounce, Complaint, Reject, Open, Click, RenderingFailure, DeliveryDelay, Subscription.

**Incremental (APScheduler, every 2 min).**

```
dirty = SELECT contact_id, channel FROM contact_profile
        WHERE profile_version > :watermark
        ORDER BY profile_version LIMIT 50000
```

Recompute windows from L2, recompute tier/score, re-evaluate against active segments for that channel. Set-based SQL over the dirty set, never a per-contact Python loop — this is what keeps §8 from being a rewrite.

**Nightly full (03:00 local).** Recompute all profiles from L2, then full segment materialization. Required, not optional: contacts fall *out* of window- and `within`-based segments with zero new events, so a dirty-set pass can never remove them. Also applies `spec_hash` and `tier_config_version` changes and repairs drift.

Order matters when `engagement.yaml` has changed: compute new tiers into a staging column, run the §4 blast-radius check, abort the whole pass on breach (leaving yesterday's membership intact — stale beats silently empty), and only then commit tiers and rematerialize segments.

Budget: 1M-row profile rebuild ≈ 30–60s; per-segment rebuild ≈ 0.3–0.5s against the partial indexes; 300 segments ≈ 2–3 min.

## 6. Event source reconciliation

Two sources emit clicks for one physical action: SES fires on redirect hit, the origin platform fires on form page load. Unreconciled, every click counts twice and `clicks_90d >= 1` trips on half the real activity.

**Canonical identity.** The SES link wrapper carries a `click_id`; the landing page echoes it in its signal. Both rows land with the same `click_id`; the later one sets `dedup_of` and does not increment counters. The first click row also inserts into `click_registry`.

**Dedup is scoped to `event_type`.** Uniqueness is `(click_id, event_type)`, never `click_id` alone. The ID propagates through the remarketing funnel to the conversion, so a conversion arrives carrying the *same* `click_id` as its originating click — collapsing on `click_id` alone would mark every attributed conversion a duplicate of its own click and drop it. This is the single easiest way to silently zero out `converts_90d`.

**Conversion attribution.** A conversion resolves its `click_id` against `click_registry` to recover `contact_id` and `campaign_id`, so the 0.20 score term and all conversion reporting are campaign-aware without a separate join path. Attribution window is `attribution_window_days` (default 30); a conversion whose click is older than the window, or whose `click_id` is unresolvable, records as unattributed rather than being discarded — an unattributed conversion is still a conversion for tiering. Multiple clicks in one campaign followed by one conversion: last touch inside the window.

**Fallback** when `click_id` is absent (unwrapped link, stripped param): collapse `(contact_id, campaign_id, event_type=click)` within a 5-minute window to one event. Log the fallback rate — a rising rate means the wrapper is losing the param somewhere, which is the same class of failure Vigil's `trace` subcommand diagnoses. Point the implementer at it rather than rebuilding the propagation logic.

**Scanner detection.** Corporate link protection (Defender Safe Links, Proofpoint, Barracuda) fetches every link in every message. Since the score is click-weighted, this inflates precisely the signal being trusted most, and it concentrates in `mailbox_provider = corporate` and `microsoft`. Classification, strongest signal first:

1. SES click with **no** origin-platform confirmation within `scanner_confirm_window_s` (default 30) → `scanner`.
2. Multiple distinct links in one message clicked within the same second → `scanner`.
3. Click earlier than 2s after delivery → `scanner`.
4. Known scanner UA / published IP ranges → `scanner`.

Rule 1 only exists because there are two sources. It is the single highest-value consequence of ingesting the origin-platform signal.

**Retroactive reclassification.** A `click_id` that later produces a conversion is proof of a human — no scanner completes a remarketing funnel. Rule 1 will misfire on background tabs, slow renders, and interstitials, and this reverses it. On an attributed conversion whose `click_registry.classification = scanner`:

1. Set `classification = human`, `reclassified_at = now()`.
2. Rewrite the originating `contact_event.classification`.
3. On that click's day row: `clicks_bot -= 1`, `clicks += 1`.
4. Recompute `last_click_at` (the reclassified click may predate the current value — take the max, never blind-assign).
5. Bump `profile_version`; the incremental pass re-tiers and re-evaluates membership.

Corrections are day-scoped to the original click date, not the conversion date, so windows and rollups stay historically accurate.

**Self-calibration.** `SELECT count(*) FROM click_registry WHERE classification = 2 AND converted_at IS NOT NULL` is a lower bound on rule 1's false-positive rate — it only catches scanner-flagged clicks that went on to convert, missing those that converted nothing. Tune `scanner_confirm_window_s` against a week of this figure rather than defending the default. This is the resolution path for Open Question 2.

**MPP opens.** SES Open events carry `ipAddress` and `userAgent`. Apple proxy ranges / Apple Mail UA → `opens_mpp`, excluded from counters **and from `last_open_at`** (Q4). Consequence to expect: `yahoo_opened_30d` gets materially smaller on the first run after this ships. That is the correction, not a regression.

**Conversions.** Origin platform only. No SES equivalent.

## 7. Segment definitions

YAML in repo, version controlled, synced to `segment_definition` on deploy. Compiled to a parameterized `WHERE` clause against `contact_profile` through a field whitelist. No SQL strings authored anywhere.

```yaml
# segments/email/yahoo_engaged.yaml
segments:
  - key: yahoo_opened_30d
    name: "Yahoo (brand) — opened in last 30 days"
    channel: email
    predicate:
      all:
        - {field: mailbox_provider, op: eq,     value: yahoo}
        - {field: last_open_at,     op: within, value: 30d}

  - key: yahoo_reputation_opened_30d
    name: "Yahoo reputation group (incl. AT&T-family, verizon.net) — opened 30d"
    channel: email
    predicate:
      all:
        - {field: reputation_group, op: eq,     value: yahoo}
        - {field: last_open_at,     op: within, value: 30d}

  - key: gmail_clickers_90d
    name: "Gmail — confirmed click in last 90 days"
    channel: email
    predicate:
      all:
        - {field: mailbox_provider, op: eq,  value: gmail}
        - {field: clicks_90d,       op: gte, value: 1}

  - key: cable_dormant_reengage
    name: "Cable — dormant, eligible for re-engagement"
    channel: email
    predicate:
      all:
        - {field: mailbox_provider, op: eq,  value: cable}
        - {field: engagement_tier,  op: eq,  value: dormant}
        - {field: sends_30d,        op: lte, value: 2}
      none:
        - {field: complaints_90d,   op: gte, value: 1}
```

Operators: `eq ne gt gte lt lte in not_in within not_within is_null is_not_null between`.
Combinators: `all` (AND), `any` (OR), `none` (NOT ANY). Nestable.

**Implicit guard.** Every segment ANDs `sendable = true` unless it declares `include_unsendable: true`. Suppression cannot be forgotten by an author.

**Cold exclusion (Q6).** When `engagement.cold_exclusion.enabled`, every segment additionally ANDs `engagement_tier != cold` unless it declares `acknowledges_cold: true`. Reputation protection is the point: repeatedly mailing contacts who have never opened is the fastest route through the Google/Yahoo 0.3% complaint ceiling.

Because this filter is silent by construction, two things are mandatory rather than nice-to-have. The compiler emits the effective predicate — including both implicit guards — into the build log, so an author can always see what actually ran. And the materialization pass records per-segment `excluded_by_guard` counts alongside membership, so "this segment is smaller than I expected" is answerable without reverse-engineering it.

```yaml
  - key: winback_never_opened
    name: "Winback — never opened, 5+ sends"
    channel: email
    acknowledges_cold: true
    predicate:
      all:
        - {field: engagement_tier, op: eq, value: cold}
      none:
        - {field: complaints_90d,  op: gte, value: 1}
```

**Predicate style.** Prefer `{last_open_at, within, 30d}` over `{opens_30d, gte, 1}`. The former is a timestamp comparison against `now()` — self-decaying and correct between rebuilds. The latter is a materialized counter, stale until the nightly pass. Reserve counters for frequency thresholds (`opens_30d >= 3`) that recency can't express.

## 8. Scaling escape hatch (100s → 1000s of segments)

Not built now. Preserved by construction:

- Eval cost scales with `segments × dirty_contacts`, not `segments × 1M`.
- Past ~500 segments, invert to predicate-indexed: extract each predicate's leading discriminator (`mailbox_provider`, `engagement_tier`), index segments by it, evaluate only candidate segments per dirty contact. No schema change.
- Roaring bitmaps (`pyroaring`, ~125KB/segment dense at 1M) only if an interactive builder lands. Out of scope.

## 9. Consumption and consent boundary

**No export step (Q9).** The local contract layer reads membership in place. Because there is no export stage left to re-check suppression at, the read surface enforces it instead:

```sql
CREATE VIEW segment_member_current AS
SELECT sm.segment_id, sd.key AS segment_key, sm.contact_id, sm.channel,
       cp.email_domain, cp.mailbox_provider, cp.reputation_group,
       cp.engagement_tier, cp.engagement_score
FROM segment_member sm
JOIN segment_definition sd USING (segment_id)
JOIN contact_profile cp ON cp.contact_id = sm.contact_id
                       AND cp.channel   = sm.channel
WHERE sm.removed_at IS NULL
  AND sd.active
  AND cp.sendable;
```

Grant the contract layer `SELECT` on this view only. **Do not grant it `segment_member` or `contact_profile`.** A suppressed contact must be unreadable, not merely unselected — this is the only remaining suppression enforcement point in the system.

**Consent is an ingest precondition (Q10).** `consent_state = 0` is not insertable and `consent_proof_id` is `NOT NULL`. A contact arriving without a resolvable consent state and proof is rejected to `contact_quarantine` with the rejection reason, never inserted as unknown. No backfill path; no legacy grandfathering.

`contact_quarantine` is reviewed by tech. Rows carry `rejected_at`, `reason`, and the raw payload. Re-entry is by re-submission through the normal ingest path once consent proof exists — quarantined rows are not promotable in place, since promoting a row is indistinguishable from manufacturing consent after the fact. Quarantine depth and oldest-row age are reported alongside the §11 checks; a growing quarantine means an upstream form is not capturing proof, which is a defect at the source rather than a backlog to work through.

## 10. SMS (phase 2)

`channel` is in the schema from day one; SMS is additive.

- Separate `contact_profile` row, separate `consent_state`. Email unsubscribe is not an SMS unsubscribe — **but complaint and hard bounce suppress across all channels** (Q5). The asymmetry is deliberate: unsubscribe is a channel preference, a spam complaint is a verdict.
- One `consent_proof` row can back both channels via `channels[]`, but only if the form's disclosure text actually covered both. `disclosure_text_hash` is what makes that defensible under a TCPA challenge, where the burden of proof is on the sender.
- STOP/HELP keyword handling and quiet hours live in send-time orchestration, **not** in segment predicates. In the segment layer they give every segment a timezone dependency and a per-minute recompute.
- `mailbox_provider` is null for SMS; carrier is the rough analogue, deferred.

## 11. Complaint and reputation monitoring

Three ceilings, three different numbers, three different consequences:

| Ceiling | Threshold | Enforced by | Consequence |
|---|---|---|---|
| Bulk sender spam rate | 0.3%, per receiving domain | Google, Yahoo | Throttling, junk foldering |
| SES complaint rate | 0.1% recommended, 0.5% pause | AWS | Account sending pause |
| SES bounce rate | 5% review, 10% pause | AWS | Account sending pause |

AWS documentation is internally inconsistent on the complaint figure — one re:Post page states 0.01% as the maximum, contradicting the FAQ's 0.1%/0.5%. Alarm at 0.1% and treat anything above as an incident; correct under either reading. SES enforcement is the existential one: Gmail throttling costs deliverability, an SES pause costs the whole channel.

### Gmail is a structural blind spot

Google operates no per-message feedback loop. Complaints from `gmail.com` recipients **never** produce an SES Complaint event. Yahoo, Microsoft, AOL, and most cable providers do run per-message ARF feedback loops, so those complaints arrive normally.

Consequences, all silent:

- `complaints_90d` reads zero for Gmail contacts regardless of true rate.
- `penalty_complaint` in the §4 score never fires for Gmail.
- A per-provider rollup built on SES events alone displays Gmail as the cleanest row in the table, for the sole reason that it is unobservable.

Two sources are therefore mandatory, not complementary:

| Source | Grain | Providers | Enables |
|---|---|---|---|
| SES Complaint events (SNS→SQS) | Per message, per address | Yahoo, Microsoft, AOL, cable | Individual suppression |
| Google Postmaster Tools API | Aggregate daily rate | Gmail only | Rate monitoring; **no** suppression |

**Feedback-ID is a send-parameter change, not a rewrite.** SES auto-generates a `Feedback-ID` header and accepts custom values within it; SES v2 `SendEmail` / `SendBulkEmail` also accept arbitrary custom headers directly, with no raw MIME construction required (`Feedback-ID` is not on SES's disallowed-custom-header list). Two viable paths, both small:

1. Supply custom values into SES's native Feedback-ID (preferred — SES owns the format).
2. Set the header explicitly via the `Headers` array on the v2 send call.

Because SES emits an auto-generated ID regardless, Gmail may already be reporting *something* under whatever granularity SES chose. The work is making it campaign-granular and tying it to `campaign_id`, not creating the data path from nothing. Reporting is volume-gated — low-volume identifiers return no data, so keep identifier cardinality low (campaign, not per-send).

**Adjacent, same ruleset:** `List-Unsubscribe` and `List-Unsubscribe-Post` are both accepted SES headers and are required by the same Google/Yahoo bulk-sender rules that set the 0.3% ceiling. One-click unsubscribe is the direct substitute for the spam button — every recipient who uses it instead is a complaint that never happens. If it isn't already on outbound, it is the cheapest complaint-rate reduction available and belongs in the same change as Feedback-ID.

**Gmail's rate has no per-contact remedy.** You cannot suppress a Gmail complainer because you are never told who they are. The only available lever is cohort-level: tighten engagement segmentation and remove the never-opened population before sending. That is precisely what `cold_exclusion` (§7) does. It is not list hygiene — it is the only Gmail complaint control in this system, and the rationale is recorded here because the first person who finds cold exclusion inconvenient will propose disabling it.

### Cold pool monitoring

`cold` is terminal, so the pool only grows. If cold accretion outpaces acquisition the addressable base shrinks monotonically and nothing in the system looks broken — sends just get quieter and every rate metric stays healthy, because the remaining population is by definition the engaged one.

Vigil check `cold_pool`, same cadence as `complaint_rate`, reporting: absolute cold count, 30d accretion, 30d new-contact acquisition, and net. Alert when net is negative for 3 consecutive checks. This is the counterweight to the reputation argument above — cold exclusion protects deliverability by spending addressable base, and the trade needs a number attached.

### Rollup

```sql
CREATE TABLE provider_daily_rollup (
  day               date     NOT NULL,
  reputation_group  smallint NOT NULL,   -- MX operator, not brand: §3
  channel           smallint NOT NULL,
  source            smallint NOT NULL,   -- 1=ses_events 2=gpt_api
  sends             int NOT NULL,
  delivers          int NOT NULL,
  bounces_hard      int NOT NULL,
  bounces_soft      int NOT NULL,
  complaints        int NOT NULL,
  complaint_rate    numeric(8,6),        -- complaints / delivers
  complaint_rate_7d  numeric(8,6),
  complaint_rate_30d numeric(8,6),
  bounce_rate       numeric(8,6),
  PRIMARY KEY (day, reputation_group, channel, source)
);
```

**Denominator is `delivers`, not `sends`.** Google measures against delivered mail; dividing by sends understates the rate by exactly your bounce rate, and does so worst on the lists that are already in trouble.

Grouped by `reputation_group`, not `mailbox_provider` — the ceiling is enforced by whoever operates the receiving MX, so AT&T-family volume must roll up under Yahoo (§3) or the Yahoo row understates.

### Ownership and cadence

Segmentation engine materializes `provider_daily_rollup` in the nightly pass; it holds write access and the L2 data. **Vigil reads it** through its existing read-only Aurora role, applies thresholds from config, and alerts on the Gauntlet webhook pattern. New Vigil check: `complaint_rate`, per `reputation_group`, warn/critical levels. This is the standing-rule case exactly — a deterministic threshold check on a timer — and it needs no new write path.

Nightly alone is the wrong resolution. A single bad send breaches in hours, and SES recalculates its own rates on a representative volume with every send rather than on a fixed window, so the rollup will never match the SES dashboard. Two-speed monitoring:

- **Fast path:** CloudWatch alarm on the SES-native reputation metric, at 0.05 bounce / 0.001 complaint. This is the number that triggers enforcement.
- **Diagnostic path:** nightly `provider_daily_rollup` + Vigil check. This is the number that tells you *which* provider and *which* campaign.

Treat the SES dashboard as authoritative for enforcement and the rollup as authoritative for attribution. Do not attempt to reconcile them.

## 12. Test scenarios

| # | Scenario | Expected |
|---|---|---|
| 1 | Opens once day 0, silent after; evaluate day 31 | Drops from `yahoo_opened_30d` on the nightly pass, not on ingest |
| 2 | SES Open arrives twice, same `provider_msg_id` | One `contact_event`; `opens` incremented once |
| 3 | Open from Apple proxy IP/UA | `opens_mpp` only; `opens` and `last_open_at` unchanged; tier unchanged |
| 4 | SES Click + origin-platform click, same `click_id` | One confirmed click; second row sets `dedup_of`; `clicks` +1 |
| 5 | SES Click, no landing signal within 30s | `clicks_bot` +1; `clicks`, `last_click_at`, score unchanged |
| 6 | 8 links in one message clicked in the same second | All → `clicks_bot`; contact does not reach `active` |
| 7 | Origin-platform click, `click_id` param stripped | 5-min fallback collapses with the SES row; fallback counter increments |
| 8 | Conversion event from remarketing funnel | Resolves `click_id` in `click_registry`; `converts` +1; `last_convert_at` set; tier → `active` |
| 8a | Conversion carries same `click_id` as its click | Both rows persist; conversion **not** marked `dedup_of` the click |
| 8b | Conversion whose click is 45d old, window 30d | Recorded unattributed; still counts toward `converts_90d` and tiering |
| 8c | Conversion with unresolvable `click_id` | Recorded unattributed; no error; logged |
| 8d | Two clicks one campaign, then one conversion | Last touch inside window gets attribution; `convert_count` +1 on that row only |
| 8e | Click flagged `scanner`, converts 3 days later | Reclassified human; `clicks_bot` -1 and `clicks` +1 **on the original click's day**; `last_click_at` = max(existing, click time); re-tiered |
| 8f | Reclassified click predates current `last_click_at` | `last_click_at` unchanged; counters still corrected |
| 9 | Complaint received on email | `suppressed_at` set on email **and** SMS rows synchronously; score → 0 |
| 10 | Contact at `sbcglobal.net` | `mailbox_provider = cable`, `reputation_group = yahoo` |
| 11 | Contact at Workspace-hosted `acme.com` | `mailbox_provider = corporate`, `reputation_group = gmail` |
| 12 | Unknown domain on ingest | Classified `other`; MX lookup enqueued; reclassified within 24h; zero added ingest latency |
| 13 | Contact insert with no `consent_proof_id` | Rejected to `contact_quarantine`; no `contact_profile` row |
| 14 | Predicate YAML edited, `spec_hash` changes | Full rebuild of that segment nightly; membership diff logged |
| 15 | Segment predicate admits `cold`, no `acknowledges_cold` | Cold contacts excluded; `excluded_by_guard` count recorded; effective predicate in build log |
| 15a | Segment declares `acknowledges_cold: true` | Cold contacts included; `sendable` guard still applied |
| 15b | `cold_min_sends_lifetime` 5 → 6, shifts 3% of base | Applied; delta logged and Slacked; segments rematerialized |
| 15c | `cold_min_sends_lifetime` 5 → 20, shifts 40% of base | Nightly pass aborts; prior tiers and membership intact; alert raised |
| 15d | Same change re-run with `--force` | Applied; full rebuild; delta logged |
| 15e | `cable` provider override set to 8 | `comcast.net` contact with 6 unopened sends is `dormant`; `gmail.com` peer is `cold` |
| 15f | `engagement.yaml` edited, no restart | No effect until nightly; `tier_config_version` mismatch drives the rebuild |
| 16 | Contact suppressed after membership computed | Absent from `segment_member_current` immediately; `segment_member` row still present |
| 17 | Opted out of email, opted in to SMS | `sendable` false on email row, true on SMS; excluded from all email segments |
| 18 | 50k events in one minute | Incremental batches at 50k; watermark advances; no contact missed |
| 19 | Nightly rebuild, zero events since last run | Windows decay; tiers demote; membership shrinks |
| 20 | Re-enters segment after removal | New `segment_member` row, new `added_at`; prior row retains `removed_at` |
| 21 | Hard bounce, then later valid delivery to same address | Suppression sticky; requires explicit manual unsuppress |
| 22 | `contact_event` partition N-14 dropped | L2 rollups and all profile fields unchanged |
| 23 | Gmail recipient marks message as spam | No SES Complaint event; contact **not** suppressed; rate visible only in GPT rollup |
| 24 | Yahoo recipient marks message as spam | SES Complaint event; suppressed on all channels synchronously; score → 0 |
| 25 | Rollup computed for a day with 100k sends, 95k delivers, 200 complaints | `complaint_rate` = 0.002105 (÷ delivers), not 0.002 (÷ sends) |
| 26 | AT&T-family volume present | Rolls into `reputation_group = yahoo` row, not a separate cable row |
| 27 | `complaint_rate_7d` crosses 0.003 for one reputation group | Vigil `complaint_rate` check fires critical; Slack alert names the group |
| 28 | GPT API unavailable for a day | Gmail row absent for `source = 2`; check reports missing data rather than passing |
| 29 | `Feedback-ID` header absent from outbound | GPT returns no campaign-level data; monitoring degrades to domain aggregate; logged as a config defect |
| 30 | Contact with 6 unopened sends, 12 days on file | **Not** `cold` — tenure guard holds; tier `dormant` |
| 31 | Contact with 6 unopened sends, 60 days on file | `cold`; excluded from all segments lacking `acknowledges_cold` |
| 32 | Contact with 3 unopened sends, 200 days on file | **Not** `cold` — send-count condition unmet |
| 33 | Cold accretion exceeds acquisition 3 checks running | Vigil `cold_pool` alerts; net figure reported with both components |
| 34 | Contact rejected to `contact_quarantine`, consent later captured | Re-submission through ingest creates the profile; quarantine row not promoted in place |

## 13. Open Questions

1. ~~**`click_id` propagation through the remarketing funnel.**~~ **Resolved:** the ID carries forward to the conversion. Conversion attribution and retroactive scanner correction both specified in §6.
2. ~~**Scanner rule 1 window.**~~ **Resolved in method, open in value:** calibrate `scanner_confirm_window_s` against a week of scanner-flagged clicks that later converted (§6, self-calibration). 30s is the starting default, not a defended number. Do not enable any suppression-adjacent behavior on rule 1 until this has run.
3. ~~**Per-provider complaint monitoring.**~~ **Resolved:** two sources, rollup owned by the segmentation engine, thresholds owned by Vigil, two-speed cadence. Specified in §11.
4. ~~**Who owns the `Feedback-ID` header?**~~ **Resolved:** added on send. Smaller than first assessed — SES supports it natively as a send parameter, no raw MIME path required (§11).
5. **Unwrapped-link rate.** The 5-min fallback is a correctness hazard if it fires often — two genuinely distinct clicks in one campaign inside 5 minutes collapse to one. Acceptable only while the fallback rate stays low. What's the expected share of unwrapped links?
6. **`attribution_window_days` value.** 30 is a placeholder. Needs to match the actual click→convert latency distribution of the remarketing funnel; too short and real conversions land unattributed, too long and campaign attribution gets noisy. First week of `click_registry` data answers it.
7. **Gmail complaint response — automated or manual?** When the GPT rate crosses threshold, does anything happen automatically (tighten `cold_min_sends_lifetime`, pause sends to `reputation_group = gmail`), or is it alert-and-human? Automated response to an aggregate metric with no per-contact attribution is a large lever with a coarse trigger. Recommend alert-only until there's a baseline.
8. ~~**Quarantine handling.**~~ **Resolved:** tech reviews; re-entry by re-submission only, no in-place promotion (§9).
9. **Per-provider threshold overrides — ship now or later?** The structure supports them and the reasoning is sound (cable/telco under-report opens, so equal thresholds aren't equal evidence), but every override is a divergence someone has to remember exists when a segment comes back the wrong size. Recommend shipping global-only and adding overrides once there's per-provider open-rate data to justify specific numbers.
10. ~~**Does `cold` ever release?**~~ **Resolved: no — terminal.** Two mitigations specified: `cold_min_tenure_days: 30` (§4) so terminality can't fire on a contact's first week, and the Vigil `cold_pool` check (§11) so monotonic base shrink stays visible. Revisit the tenure value if send cadence changes materially — see §4 note on the cadence interaction.
11. **Where does `max_tier_shift_pct` enforcement live?** In the nightly pass as specified, or as a Vigil check with the nightly pass reading its verdict? Vigil owns deterministic threshold checks with an existing Slack path; the argument for keeping it inline is that it must block the commit, not report on it after the fact.
12. **`click_registry` retention.** Currently implied 13 months to match `contact_event`. But reclassification and attribution only need the attribution window plus slack — a much shorter retention (say 90d) keeps the table small, at the cost of losing long-tail conversion attribution. Which matters more?
13. **Is `List-Unsubscribe` already on outbound?** Required by the same bulk-sender rules as the 0.3% ceiling, and the cheapest complaint-rate reduction available (§11). Outside this spec's scope but worth confirming before tuning anything else against complaint rate.
