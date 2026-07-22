# Remarketing ESP — Technical Specification

> **For Claude Code**: This is a greenfield Python/FastAPI project. Build incrementally, ship in milestones (see end of doc). Default to patterns from the existing Gauntlet codebase (Python/FastAPI, APScheduler, Aurora Postgres, multi-tenant config). Ask before introducing new infra (Redis, queues, etc.) — Postgres-only is the explicit constraint for v1.

---

## 1. Product Overview

A standalone Email Service Provider (ESP) for OTTO auto insurance remarketing. Replaces the need for a third-party platform like ExpertSender. Supports:

- Behavioral workflow automation (visual builder + YAML)
- AI-generated email copy with cohort-level personalization
- Multi-Armed Bandit (MAB) optimization across template variants
- Real-time deliverability dashboard
- Suppression list management with bidirectional OTTO sync
- Webhook ingestion of opens/clicks/bounces/unsubscribes from ESP

**Scale targets**: 100K–1M sends/month at launch. Single dedicated SES IP. Single tenant (OTTO) for v1, but architect for multi-tenant (OTTO + Giraffe + future) as a v2 lift.

**Out of scope for v1**: Multi-ESP failover, send-time optimization (STO), A/B test framework outside MAB, drag-and-drop email designer (use HTML templates).

---

## 2. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Matches Gauntlet |
| API framework | FastAPI | Matches Gauntlet |
| Scheduler | APScheduler | Matches Gauntlet |
| DB | Aurora Postgres 15+ | User constraint, matches Gauntlet |
| ORM | SQLAlchemy 2.x + Alembic | Standard |
| ESP | AWS SES (boto3), Managed Dedicated IPs | Starting point; abstract behind interface. See SPEC_ADDENDUM.md 001 |
| AI | Anthropic Claude API (Sonnet for generation, Haiku for classification) | Matches existing tooling |
| Frontend | React 18 + TypeScript + Vite + react-flow | react-flow is the standard for node-based builders |
| Frontend styling | Tailwind + shadcn/ui | Fast iteration |
| Auth | Internal SSO (whatever OTTO uses) — JWT validation | TBD with user |
| Deployment | Docker, target whatever Gauntlet uses (likely ECS/Fargate) | TBD |

**Explicit non-choices (do not add without asking):**
- ❌ Redis / Memcached — Postgres only for v1
- ❌ Kafka / SQS / RabbitMQ — use Postgres `LISTEN/NOTIFY` or APScheduler-driven polling
- ❌ ClickHouse / Timescale — Postgres with proper partitioning for events
- ❌ Other ESPs — SES only for v1, but adapter interface must exist

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         React Frontend                          │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐   │
│  │  Workflow   │  │ Deliverabil │  │  Campaign / Template │   │
│  │  Builder    │  │ ity Dashbd  │  │  Management UI       │   │
│  └─────────────┘  └─────────────┘  └──────────────────────┘   │
└────────────────────────────┬────────────────────────────────────┘
                             │ REST + SSE
┌────────────────────────────▼────────────────────────────────────┐
│                      FastAPI Application                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │ Ingest   │ │ Workflow │ │ Suppres  │ │  Webhook │ │ Admin│ │
│  │ API      │ │ API      │ │ API      │ │  Endpts  │ │ API  │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────┘ │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                  Background Workers (APScheduler)               │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────┐│
│  │ Workflow     │ │ AI Variant   │ │ Send Worker  │ │ Otto   ││
│  │ Tick (10s)   │ │ Generator    │ │ (rate-lmtd)  │ │ Sync   ││
│  └──────────────┘ └──────────────┘ └──────────────┘ └────────┘│
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐           │
│  │ Stats Roll-up│ │ Bounce/Compl │ │ Reputation   │           │
│  │ (5 min)      │ │ Handler      │ │ Monitor      │           │
│  └──────────────┘ └──────────────┘ └──────────────┘           │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                      Aurora Postgres                            │
│  recipients, journeys, workflows, campaigns, templates,         │
│  variants, sends, events (partitioned), suppressions,           │
│  variant_stats (matview), unsub_outbox                          │
└────────────────────────────┬────────────────────────────────────┘
                             │
                ┌────────────┴───────────┐
                │                        │
       ┌────────▼────────┐      ┌────────▼────────┐
       │   AWS SES       │      │   OTTO API      │
       │  (send + SNS)   │      │  (lead data,    │
       │                 │      │   unsub sync)   │
       └─────────────────┘      └─────────────────┘
```

### 3.1 Service boundaries

Single FastAPI monolith with worker processes. **Do not** split into microservices for v1. Use Python modules organized by domain:

```
app/
├── api/              # FastAPI routers
│   ├── ingest.py
│   ├── workflows.py
│   ├── campaigns.py
│   ├── templates.py
│   ├── suppressions.py
│   ├── webhooks.py   # SES SNS endpoint, OTTO unsub endpoint
│   ├── stats.py
│   └── admin.py
├── core/
│   ├── config.py     # Pydantic settings
│   ├── db.py         # SQLAlchemy session mgmt
│   ├── auth.py
│   └── logging.py
├── domain/           # Business logic (no FastAPI/SQLAlchemy imports)
│   ├── workflow_engine.py
│   ├── mab.py        # Thompson sampling
│   ├── personalization.py
│   ├── suppression.py
│   └── compliance.py # CAN-SPAM, RFC 8058
├── adapters/
│   ├── esp/
│   │   ├── base.py   # ESPAdapter ABC
│   │   ├── ses.py
│   │   └── (mailgun.py, sendgrid.py — stubs)
│   ├── ai/
│   │   └── anthropic.py
│   └── otto/
│       └── client.py
├── models/           # SQLAlchemy models
├── workers/          # APScheduler jobs
│   ├── scheduler.py
│   ├── workflow_tick.py
│   ├── send_worker.py
│   ├── stats_rollup.py
│   ├── otto_sync.py
│   └── ai_generator.py
├── schemas/          # Pydantic
└── main.py
```

---

## 4. Data Model

All tables include `id` (UUID v7 — sortable), `created_at`, `updated_at`. Use UUID v7 for all primary keys via `uuid_generate_v7()` extension or app-side generation.

**Tenant scoping:** tenant-owned tables (recipients, suppressions, unsub_outbox, workflows, journeys, campaigns, templates, sends, events) include a `tenant text NOT NULL` column. V1 uses `'otto'` everywhere; the column exists so a second tenant (Giraffe, etc.) can be onboarded in v2 without schema churn. Unique constraints that reference `email` are keyed on `(tenant, email)` so each tenant maintains its own address space.

### 4.1 Core tables

```sql
-- Recipients = email addresses we can send to. Deduplicated by (tenant, email).
recipients (
  id              uuid PK,
  tenant          text NOT NULL,    -- 'otto' in v1; scoping key for multi-tenant
  email           citext NOT NULL,
  external_id     text,             -- tenant-side lead id (OTTO lead_id for tenant='otto')
  vertical        text NOT NULL,    -- 'auto' | 'home'
  first_seen_at   timestamptz NOT NULL,
  last_seen_at    timestamptz NOT NULL,
  consent_source  text NOT NULL,    -- 'otto_form' for v1
  consent_at      timestamptz NOT NULL,
  attributes      jsonb NOT NULL DEFAULT '{}',  -- See §5 for schema
  status          text NOT NULL DEFAULT 'active', -- active|suppressed|bounced_hard|complained
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL,
  UNIQUE (tenant, email)
);
CREATE INDEX ON recipients (status, vertical);
CREATE INDEX ON recipients USING gin (attributes);

-- Workflows = the visual flowchart definitions.
workflows (
  id              uuid PK,
  name            text NOT NULL,
  vertical        text NOT NULL,
  status          text NOT NULL,    -- draft|active|paused|archived
  definition      jsonb NOT NULL,   -- See §6 for schema
  entry_segment   jsonb,            -- Optional filter for new ingest
  version         int NOT NULL DEFAULT 1,
  created_by      text,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL
);

-- Journey instances = a recipient progressing through a workflow.
journeys (
  id              uuid PK,
  workflow_id     uuid NOT NULL REFERENCES workflows(id),
  workflow_version int NOT NULL,    -- pin version at entry
  recipient_id    uuid NOT NULL REFERENCES recipients(id),
  current_node_id text NOT NULL,    -- node id within definition
  next_action_at  timestamptz,      -- null = waiting on event
  status          text NOT NULL,    -- active|completed|exited|errored
  context         jsonb NOT NULL DEFAULT '{}', -- per-journey state
  entered_at      timestamptz NOT NULL,
  completed_at    timestamptz,
  UNIQUE (workflow_id, recipient_id)  -- one active journey per recip per wf
);
CREATE INDEX ON journeys (status, next_action_at) WHERE status = 'active';
CREATE INDEX ON journeys (recipient_id);

-- Campaigns = a logical send event tied to a workflow node.
campaigns (
  id              uuid PK,
  workflow_id     uuid REFERENCES workflows(id),
  node_id         text,             -- node within workflow that emits this
  name            text NOT NULL,
  vertical        text NOT NULL,
  status          text NOT NULL,    -- draft|generating|ready|active|paused|archived
  cohort_filter   jsonb,            -- which recipient attrs define cohorts
  ai_brief        text,             -- prompt context for AI generation
  mab_config      jsonb NOT NULL,   -- {algo: 'thompson', explore_pct: 0.1}
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL
);

-- Templates = HTML/text shells with merge tags. AI fills these.
templates (
  id              uuid PK,
  campaign_id     uuid NOT NULL REFERENCES campaigns(id),
  cohort_key      text NOT NULL,    -- hash of cohort attrs, e.g. "auto:TX:35-44"
  variant_label   text NOT NULL,    -- 'A','B','C',...
  subject         text NOT NULL,    -- with {{merge_tags}}
  preheader       text,
  body_html       text NOT NULL,
  body_text       text NOT NULL,
  ai_meta         jsonb,            -- model, prompt_version, generated_at, tokens
  status          text NOT NULL,    -- generated|approved|rejected|retired
  created_at      timestamptz NOT NULL,
  UNIQUE (campaign_id, cohort_key, variant_label)
);
CREATE INDEX ON templates (campaign_id, cohort_key, status);

-- Sends = one row per recipient × send attempt.
sends (
  id              uuid PK,
  journey_id      uuid REFERENCES journeys(id),
  campaign_id     uuid NOT NULL REFERENCES campaigns(id),
  template_id     uuid NOT NULL REFERENCES templates(id),
  recipient_id    uuid NOT NULL REFERENCES recipients(id),
  cohort_key      text NOT NULL,
  variant_label   text NOT NULL,
  esp             text NOT NULL,    -- 'ses'
  esp_message_id  text,             -- SES messageId for webhook joining
  rendered_subject text NOT NULL,   -- post-merge, for audit
  status          text NOT NULL,    -- queued|sent|delivered|bounced|complained|failed
  queued_at       timestamptz NOT NULL,
  sent_at         timestamptz,
  delivered_at    timestamptz,
  failure_reason  text
);
CREATE INDEX ON sends (esp_message_id);
CREATE INDEX ON sends (recipient_id, sent_at DESC);
CREATE INDEX ON sends (campaign_id, variant_label, sent_at);

-- Events = opens, clicks, bounces, complaints, unsubs. Partitioned by day.
CREATE TABLE events (
  id              uuid NOT NULL,
  send_id         uuid REFERENCES sends(id),
  recipient_id    uuid NOT NULL,
  campaign_id     uuid,
  variant_label   text,
  cohort_key      text,
  event_type      text NOT NULL,    -- open|click|bounce|complaint|unsubscribe|delivery|reject
  event_subtype   text,             -- bounce: 'hard'|'soft'; click: url
  url             text,             -- for clicks
  user_agent      text,
  ip_address      inet,
  raw_payload     jsonb,
  occurred_at     timestamptz NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);
-- pg_partman or a cron job manages monthly partitions.
CREATE INDEX ON events (send_id);
CREATE INDEX ON events (recipient_id, occurred_at DESC);
CREATE INDEX ON events (event_type, occurred_at DESC);

-- Suppressions = per-tenant DNE list. Sources include unsubs, bounces, complaints, OTTO push.
suppressions (
  id              uuid PK,
  tenant          text NOT NULL,
  email           citext NOT NULL,
  reason          text NOT NULL,    -- unsubscribe|hard_bounce|complaint|manual|otto_push
  source          text NOT NULL,    -- workflow_id, 'sns', 'otto', 'admin_user'
  campaign_id     uuid,
  notes           text,
  suppressed_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant, email)
);
CREATE INDEX ON suppressions USING hash (email);  -- narrow by tenant at app layer

-- Outbox for pushing unsubscribes back to the owning tenant (OTTO for v1).
unsub_outbox (
  id              uuid PK,
  tenant          text NOT NULL,
  email           citext NOT NULL,
  reason          text NOT NULL,
  occurred_at     timestamptz NOT NULL,
  attempts        int NOT NULL DEFAULT 0,
  last_attempt_at timestamptz,
  delivered_at    timestamptz,
  last_error      text
);
CREATE INDEX ON unsub_outbox (delivered_at, attempts) WHERE delivered_at IS NULL;

-- MAB stats — materialized view refreshed every 5 min.
CREATE MATERIALIZED VIEW variant_stats AS
SELECT
  s.campaign_id,
  s.cohort_key,
  s.variant_label,
  count(*) FILTER (WHERE s.status IN ('sent','delivered','bounced','complained')) AS sent,
  count(*) FILTER (WHERE s.delivered_at IS NOT NULL) AS delivered,
  count(DISTINCT e.recipient_id) FILTER (WHERE e.event_type='open') AS unique_opens,
  count(DISTINCT e.recipient_id) FILTER (WHERE e.event_type='click') AS unique_clicks,
  count(*) FILTER (WHERE e.event_type='bounce') AS bounces,
  count(*) FILTER (WHERE e.event_type='complaint') AS complaints,
  count(*) FILTER (WHERE e.event_type='unsubscribe') AS unsubs,
  max(s.sent_at) AS last_sent_at
FROM sends s
LEFT JOIN events e ON e.send_id = s.id
GROUP BY 1,2,3;
CREATE UNIQUE INDEX ON variant_stats (campaign_id, cohort_key, variant_label);
```

### 4.2 Event partitioning

- Monthly partitions, 13-month retention (12 active + current).
- Cron job in `workers/partition_manager.py` runs daily, creates next month if missing, drops partitions > 13 months old.
- Use `pg_partman` if comfortable; otherwise hand-rolled is fine.

### 4.3 Suppression check performance

At 1M/mo (~30 sends/sec peak), a hash index on `suppressions.email` gives sub-millisecond lookups. **No Redis needed.** Re-evaluate at 5M+/mo.

---

## 5. Personalization Schema

`recipients.attributes` is a flexible JSONB blob. The following fields are first-class for v1 (auto vertical only — home is v2):

### 5.1 Auto insurance attributes

```jsonc
{
  // Identity
  "first_name": "Sarah",
  "last_name": "Johnson",
  "age_band": "35-44",          // 18-24, 25-34, 35-44, 45-54, 55-64, 65+

  // Geography
  "state": "TX",
  "zip": "75201",
  "city": "Dallas",
  "metro": "Dallas-Fort Worth",

  // Vehicle (primary)
  "vehicle_year": 2018,
  "vehicle_make": "Honda",
  "vehicle_model": "Civic",
  "vehicle_age_band": "5-10yr",
  "multi_vehicle": false,
  "vehicle_count": 1,

  // Insurance status
  "current_carrier": "GEICO",
  "current_premium_band": "100-150",  // monthly USD bands
  "coverage_type": "full",            // full|liability|unknown
  "homeowner": true,                  // for bundle hooks
  "credit_band": "good",              // excellent|good|fair|poor|unknown

  // Lead lifecycle
  "lead_age_days": 12,
  "first_quoted_at": "2026-04-08T...",
  "quote_count": 3,
  "last_quote_carrier": "Progressive",

  // Derived (computed nightly by enrichment job)
  "estimated_savings_band": "20-50",  // $/mo savings vs current_premium_band
  "carriers_in_zip": 12,
  "in_renewal_window": false          // 30 days before policy renewal if known
}
```

### 5.2 Cohort definition

A cohort is a tuple of (vertical, state, age_band, vehicle_age_band, current_carrier presence). Hashed deterministically into `cohort_key`:

```
cohort_key = sha1(f"{vertical}|{state}|{age_band}|{vehicle_age_band}|{has_carrier}")[:12]
```

Campaigns specify which attributes define their cohorts via `cohort_filter`. AI generates one set of variants per cohort.

### 5.3 Merge tag rendering

Templates use Jinja2-style `{{ attribute }}` with safe defaults:

```
Hi {{ first_name | default("there") }},

We found {{ carriers_in_zip | default("multiple") }} carriers serving
{{ city | default("your area") }} that could save you up to
${{ estimated_savings_band | savings_high | default("XX") }}/month.
```

Custom Jinja filters live in `domain/personalization.py`:
- `savings_high`, `savings_low` — extract bounds from band strings
- `carrier_or` — fallback to "your current carrier"
- `vehicle_descriptor` — "2018 Honda" or "your vehicle"

**If a merge tag's value is missing AND no default is provided, the send is rejected and logged.** No empty `{{ }}` placeholders should ever ship.

---

## 6. Workflow Engine

### 6.1 Workflow definition (JSON)

```jsonc
{
  "version": 1,
  "name": "Auto - 7 day nurture",
  "vertical": "auto",
  "entry": "node_start",
  "nodes": {
    "node_start": {
      "type": "entry",
      "next": "node_send_1"
    },
    "node_send_1": {
      "type": "send",
      "campaign_id": "uuid-of-campaign",
      "next": "node_wait_1"
    },
    "node_wait_1": {
      "type": "wait",
      "duration": "P3D",          // ISO 8601 duration
      "next": "node_branch_1"
    },
    "node_branch_1": {
      "type": "branch",
      "condition": {
        "kind": "event_in_window",
        "event_type": "open",
        "since_node": "node_send_1",
        "window": "P3D"
      },
      "if_true": "node_send_engaged",
      "if_false": "node_send_reengage"
    },
    "node_send_engaged": {
      "type": "send",
      "campaign_id": "uuid-2",
      "next": "node_wait_final"
    },
    "node_send_reengage": {
      "type": "send",
      "campaign_id": "uuid-3",
      "next": "node_wait_final"
    },
    "node_wait_final": {
      "type": "wait",
      "duration": "P4D",
      "next": "node_exit"
    },
    "node_exit": {
      "type": "exit",
      "reason": "completed"
    }
  }
}
```

### 6.2 Node types (v1)

| Type | Purpose | Required fields |
|---|---|---|
| `entry` | Start of workflow | `next` |
| `send` | Send a campaign | `campaign_id`, `next` |
| `wait` | Delay | `duration` (ISO 8601), `next` |
| `branch` | Conditional split | `condition`, `if_true`, `if_false` |
| `exit` | Terminate journey | `reason` |

### 6.3 Branch conditions (v1)

Behavioral only per user requirement:

```jsonc
{ "kind": "event_in_window",
  "event_type": "open" | "click" | "any_engagement",
  "since_node": "node_id",       // or "any_send" or "journey_start"
  "window": "P3D" }              // ISO 8601 duration

{ "kind": "click_url_match",
  "since_node": "node_id",
  "url_contains": "/quote",
  "window": "P7D" }

{ "kind": "and" | "or",
  "operands": [ ...subconditions ] }
```

### 6.4 Workflow tick worker

Runs every 10 seconds. Single-process; uses `SELECT ... FOR UPDATE SKIP LOCKED` for safe concurrent worker scaling later.

```python
# Pseudocode
def tick():
    now = utcnow()
    journeys = db.execute("""
        SELECT * FROM journeys
        WHERE status = 'active'
          AND next_action_at <= :now
        ORDER BY next_action_at
        LIMIT 1000
        FOR UPDATE SKIP LOCKED
    """, now=now)

    for j in journeys:
        wf = load_workflow(j.workflow_id, j.workflow_version)
        node = wf.nodes[j.current_node_id]
        try:
            result = execute_node(node, j, wf)
            advance_journey(j, result)
        except Exception as e:
            mark_journey_errored(j, e)
            log.exception(...)
```

### 6.5 Re-entry policy

A recipient cannot have two active journeys in the same workflow simultaneously (enforced by unique constraint). They CAN be in multiple different workflows. Workflow re-entry after completion: configurable per workflow with a cooldown (default: never re-enter, or `P30D` cooldown).

### 6.6 Exit conditions (global)

Every tick also checks global exit conditions for active journeys. If any are true, journey is exited:

- Recipient is suppressed
- Recipient hard-bounced
- Recipient complained
- Workflow is paused or archived

---

## 7. AI Generation + MAB

### 7.1 Generation pipeline

When a campaign is created or its `ai_brief` is updated:

1. Compute the set of cohorts in scope (cross product of `cohort_filter` against actual recipient population).
2. For each cohort, queue a generation job.
3. AI worker calls Claude API with a structured prompt including:
   - Brand voice guide (stored as a versioned config)
   - Cohort description (state, age band, vehicle age, carrier status)
   - Top-performing historical headlines (pulled from your existing OTTO native ad analysis: Loss Aversion > Comparison Empowerment > FOMO > Curiosity > Deal Seeking)
   - Compliance constraints (no fake urgency, no false savings claims, must include unsubscribe)
   - Available merge tags
   - Number of variants requested (default: 5)
4. Output is structured JSON: `[{subject, preheader, body_html, body_text, emotional_driver}, ...]`
5. Each variant is stored with `status='generated'`. By default, requires human approval before going live (configurable per campaign as `auto_approve: true`).

### 7.2 Prompt versioning

Store prompts as files under `prompts/` with semantic versioning. `templates.ai_meta` records `prompt_version` so you can correlate performance to prompt changes.

### 7.3 MAB selection

**Thompson sampling over Beta distributions** keyed on (campaign_id, cohort_key). Reward signal: click (primary) or open (fallback if click volume too low).

```python
def select_variant(campaign_id, cohort_key) -> variant_label:
    stats = db.fetch("""
        SELECT variant_label, unique_clicks, sent
        FROM variant_stats
        WHERE campaign_id = :c AND cohort_key = :k
    """, c=campaign_id, k=cohort_key)

    # Forced exploration: minimum sends per variant before MAB kicks in
    underexplored = [s for s in stats if s.sent < MIN_EXPLORE_SENDS]  # e.g. 200
    if underexplored:
        return random.choice(underexplored).variant_label

    # Thompson sampling
    samples = []
    for s in stats:
        alpha = 1 + s.unique_clicks
        beta  = 1 + (s.sent - s.unique_clicks)
        samples.append((random.betavariate(alpha, beta), s.variant_label))

    return max(samples)[1]
```

### 7.4 Variant lifecycle

- `generated` — created by AI, awaiting approval
- `approved` — eligible for MAB selection
- `rejected` — manually killed
- `retired` — auto-retired when:
  - Click rate is statistically significantly worse than best variant after `MIN_RETIRE_SENDS` (e.g., 1000), OR
  - Bounce rate > 2% on this variant specifically, OR
  - Complaint rate > 0.1%

Retirement runs as part of the stats roll-up worker.

### 7.5 Cold-start handling

When a new campaign launches with no historical data:
- Forced uniform exploration for first `MIN_EXPLORE_SENDS` per variant per cohort (default 200).
- After exploration phase, Thompson sampling activates.
- Show "exploring" badge in UI for variants in this phase.

---

## 8. ESP Adapter Layer

### 8.1 Interface

```python
# app/adapters/esp/base.py
class ESPAdapter(ABC):
    @abstractmethod
    async def send(self, message: OutboundMessage) -> SendResult: ...

    @abstractmethod
    async def get_reputation(self) -> ReputationSnapshot: ...

    @abstractmethod
    def parse_webhook(self, payload: dict, signature: str | None) -> list[ParsedEvent]: ...

    @abstractmethod
    async def add_to_esp_suppression(self, email: str, reason: str) -> None: ...
```

### 8.2 SES adapter (v1)

- `send`: uses SES v2 `SendEmail` API. Configuration set name from config.
- `get_reputation`: SES `GetAccountSendingEnabled` + CloudWatch metrics for bounce/complaint rates. Pulls per-ISP metrics from CloudWatch for the managed pool (`Reputation.IspFeedback`, `Managed.DedicatedIpCount`, `Managed.SendingAllowed`) — see SPEC_ADDENDUM.md 001 §2.4.
- `parse_webhook`: validates SNS signature, parses Notification → list of events.
- `add_to_esp_suppression`: uses SES account-level suppression list as second line of defense.

### 8.3 Webhook ingestion

`POST /webhooks/ses` — accepts SNS notifications. Subscription confirmation auto-handled. Body parsed, events written to `events` table, then status on `sends` row updated synchronously. SES messageId joins events to sends.

Bounces (hard) and complaints automatically:
1. Insert into `suppressions`
2. Insert into `unsub_outbox` to push back to OTTO
3. Update `recipients.status`
4. Active journeys for this recipient are exited on next workflow tick

---

## 9. OTTO Integration

### 9.1 Inbound: lead ingestion

`POST /api/ingest/recipients` accepts a JSON array of leads. Idempotent on `email`. Triggers:

- Upsert into `recipients`
- For each active workflow whose `entry_segment` matches the recipient, create a `journey` row (if not already in that workflow)

```jsonc
// Request
{
  "recipients": [
    {
      "email": "sarah@example.com",
      "external_id": "otto_lead_12345",
      "vertical": "auto",
      "consent_at": "2026-04-21T14:23:00Z",
      "consent_source": "otto_form",
      "attributes": { /* §5.1 */ }
    }
  ]
}
```

Also support `POST /api/ingest/aged` for bulk re-ingestion of older leads (different default workflow targeting).

### 9.2 Outbound: unsubscribe sync

`unsub_outbox` worker runs every 60s. For each pending row:
1. POST to OTTO unsub endpoint (TBD with user — get URL/auth)
2. On success: set `delivered_at`
3. On failure: increment `attempts`, exponential backoff (1m, 5m, 30m, 2h, 6h), give up at 10 attempts and alert

### 9.3 Inbound: OTTO suppression push

`POST /api/suppressions/push` for OTTO to inform us that a user unsubscribed elsewhere. Insert into `suppressions` with `source='otto_push'`. Authenticate via shared secret in header.

---

## 10. Compliance

User confirmed: consent is captured at OTTO form level, OTTO handles CAN-SPAM physical address — but **this system still enforces**:

- **Unsubscribe link** in every email body, generated as signed token URL
- **List-Unsubscribe header** (RFC 2369)
- **List-Unsubscribe-Post header** with `List-Unsubscribe=One-Click` (RFC 8058) — required by Gmail/Yahoo for senders > 5K/day
- **Suppression check at send time** — final guard immediately before SES call
- **Physical address** in template footer (configurable per workflow, defaults to OTTO address)

Unsub URL pattern: `https://{esp_domain}/u/{signed_token}` where token encodes recipient_id + send_id + signature. One-click POST endpoint at same URL.

---

## 11. Frontend

### 11.1 Workflow builder

- React 18 + TypeScript + Vite
- `react-flow` for the canvas
- Custom node types matching §6.2 (Send, Wait, Branch, Exit)
- Right-side property panel for node config
- Top bar: workflow name, status (draft/active/paused), version, save/publish buttons
- Validation on save: graph must be acyclic from entry to all exits, all branches must connect, all `send` nodes must reference real campaigns
- Workflow JSON rendered to/from react-flow `nodes` + `edges` arrays

### 11.2 Deliverability dashboard

Single page, top-level metrics + drilldowns. Real-time via SSE (`/api/stats/stream`).

**Top cards** (last 24h / 7d / 30d toggle):
- Sends, Deliveries, Delivery rate
- Open rate (unique), Click rate (unique)
- Bounce rate (with hard/soft split)
- Complaint rate (red flag if > 0.1%)
- Unsubscribe rate

**Charts:**
- Sends over time (hourly buckets)
- Bounce/complaint rate over time (red horizontal line at SES thresholds: 5% bounce, 0.1% complaint)
- Top 10 campaigns by volume with metrics

**SES reputation panel:**
- Current reputation (from CloudWatch)
- Account sending status
- Daily quota usage

**Per-ISP reputation (Managed Dedicated IPs — SPEC_ADDENDUM.md 001 §2.4):**
- Per-ISP reputation gauges (Gmail, Yahoo, Outlook, Apple iCloud as minimum tiles)
- Per-ISP delivery rate from our own `events` data, joined against the ISP reputation signal
- Effective daily rate per top ISP from CloudWatch

### 11.3 Other screens

- Campaign list + detail (variants, MAB stats, approve/reject/retire variants)
- Template preview (with mock recipient data for merge tag testing)
- Recipient lookup (by email — show journey history, sends, events)
- Suppression list (search, manual add, export)
- Workflow list

---

## 12. Configuration

Use Pydantic settings, env var prefix `CADENCE_`. Sensitive values from AWS Secrets Manager in prod.

```python
class Settings(BaseSettings):
    database_url: str
    aws_region: str = "us-east-1"
    ses_configuration_set: str
    ses_sending_domain: str         # e.g. mail.otto.com
    ses_from_address: str

    anthropic_api_key: str
    anthropic_generation_model: str = "claude-sonnet-4-5"
    anthropic_classification_model: str = "claude-haiku-4-5"

    otto_api_base_url: str
    otto_api_key: str
    otto_unsub_endpoint: str

    unsub_signing_key: str
    sns_topic_arn_ses_events: str
    ses_managed_pool_name: str      # managed IP pool attached to the config set at infra setup

    workflow_tick_interval_seconds: int = 10
    send_rate_limit_per_second: int = 14   # SES default account-level cap
    stats_refresh_interval_seconds: int = 300

    mab_min_explore_sends: int = 200
    mab_min_retire_sends: int = 1000
```

---

## 13. Send Rate Limiting & SES Quotas

- SES sandbox: 200/day, 1/sec — only for dev. Must request production access early.
- Production starting quota: typically 50K/day, 14/sec — request increase before launch.
- Send worker enforces per-second limit via token bucket in Postgres advisory lock or simple sleep loop.
- Per-day quota tracked in DB; sends queue if quota exhausted, resume next day.
- Per-recipient throttling: max 1 send per recipient per 24h.

Dedicated IP warmup is handled by **SES Managed Dedicated IPs** (SPEC_ADDENDUM.md 001) — SES warms per-ISP adaptively and spills to the shared pool during ramp. No warmup schedule, per-day warmup caps, or IP selection logic in the send worker.

---

## 14. Observability

- Structured logging (JSON) via `structlog`. Always include `journey_id`, `recipient_id`, `campaign_id`, `send_id` where applicable.
- Metrics: prometheus_client, exposed on `/metrics`. Track:
  - `esp_sends_total{status}`
  - `esp_send_duration_seconds`
  - `esp_workflow_journeys_active`
  - `esp_workflow_tick_duration_seconds`
  - `esp_ai_generation_duration_seconds`
  - `esp_otto_sync_failures_total`
  - Per-ISP reputation from CloudWatch (`Reputation.IspFeedback`)
  - `Managed.DedicatedIpCount` (managed pool IP count — ops signal for volume growth)
  - `Managed.SendingAllowed` (managed pool health)
- Tracing: OpenTelemetry, optional for v1 but instrument send + tick paths.
- Alerts (Slack via webhook):
  - Bounce rate > 3% over 1h
  - Complaint rate > 0.05% over 1h
  - SES reputation drops to "danger"
  - `unsub_outbox` has > 100 undelivered after 1h
  - Workflow tick lag > 60s
  - AI generation failures > 5/hr

---

## 15. Testing

- Unit tests on `domain/` modules — must be DB-free.
- Integration tests with Postgres (testcontainers) for engine + suppression + ingestion.
- End-to-end flow test: ingest recipient → workflow advances → mock ESP send → mock webhook → branch executes correctly.
- Load test: 1M synthetic recipients, 5 workflows active, verify tick keeps up and send rate hits target.

Use `pytest` + `pytest-asyncio` + `httpx` for API tests + `factory_boy` for fixtures.

---

## 16. Security

- All API endpoints require auth. Internal SSO (TBD) for UI; API key (per-service) for OTTO.
- Webhook endpoints validate SNS signature (SES) and HMAC shared secret (OTTO).
- Unsub one-click endpoint verifies signed token, accepts only POST per RFC 8058 — but also supports GET for the in-email link.
- Secrets via AWS Secrets Manager; never in env files in prod.
- PII: emails are stored unencrypted (necessary for sending). `attributes` JSONB may contain PII — restrict DB access, audit log queries against `recipients` table.

---

## 17. Build Plan (Milestones)

> Each milestone should be shippable on its own. Check in with user at end of each.

### Milestone 1: Foundations (week 1–2)
- Repo setup, FastAPI skeleton, Postgres + Alembic
- Models for `recipients`, `suppressions`, `unsub_outbox`
- Ingestion API (`POST /api/ingest/recipients`)
- Suppression CRUD API
- OTTO unsub outbox worker (mock OTTO endpoint)
- Auth middleware
- Test harness

### Milestone 2: Sending core (week 3–4)
- ESP adapter interface + SES implementation
- Templates + Campaigns models
- Send worker with rate limiting
- Webhook endpoint for SES SNS
- Events table with partitioning
- Suppression check at send time
- One-click unsub endpoint (RFC 8058)
- Ability to manually create a campaign + template + send to a list (no workflow yet)

### Milestone 3: Workflow engine (week 5–6)
- Workflows + journeys models
- Workflow tick worker
- All node types (entry, send, wait, branch, exit)
- Behavior-based branch conditions
- Global exit conditions
- API for workflow CRUD
- Inject recipients into matching workflows on ingest

### Milestone 4: AI generation + MAB (week 7–8)
- Anthropic adapter
- Cohort computation
- AI generation worker + prompt files
- Variant approval API
- Thompson sampling selector
- Stats materialized view + roll-up worker
- Variant retirement logic

### Milestone 5: Frontend (week 9–11)
- React app scaffolding (Vite + Tailwind + shadcn)
- Workflow builder (react-flow)
- Campaign + template management UI
- Deliverability dashboard (SSE)
- Recipient lookup
- Suppression list UI

### Milestone 6: Production hardening (week 12)
- SES Managed Dedicated IP pool integration (no manual warmup — SPEC_ADDENDUM.md 001)
- Reputation monitoring + alerts
- Load test to 1M/mo throughput
- Deployment automation
- Runbook documentation

---

## 18. Open Items (need user input before / during build)

1. **OTTO API details**: base URL, auth scheme, unsub push endpoint contract, lead ingestion contract (if push) or polling spec (if pull).
2. **Auth/SSO**: which provider does OTTO use? Cognito / Auth0 / custom JWT?
3. **Sending domain**: confirm subdomain (e.g. `mail.otto-insurance.com`), DKIM/SPF/DMARC setup will need DNS access.
4. **Brand voice guide**: needed for AI generation prompt — ideally pull existing OTTO ad copy guide if one exists.
5. **Deployment target**: ECS Fargate? EKS? EC2? Match Gauntlet.
6. **Slack webhook URL** for alerts.
7. **Physical mailing address** for footer (CAN-SPAM).

---

## 19. Explicit Non-Goals (v1)

To prevent scope creep — these are deferred to v2+:

- Multi-tenant isolation (Giraffe support)
- Multi-ESP failover or routing
- Per-recipient AI generation (cohort-level only)
- Send-time optimization
- A/B test framework outside MAB
- Drag-and-drop email designer (HTML templates only)
- SMS / push channels
- Predictive churn modeling
- Custom segmentation builder UI (use SQL filters in JSON for now)
- Webhook outbound (other than OTTO unsub sync)
- Audit log UI (logs only)
