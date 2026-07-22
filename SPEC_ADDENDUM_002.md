# SPEC Addendum 002 — Frequency Governance, Engagement Decay, Segment-Driven Sends

**Status**: Draft — awaiting approval. Extends SPEC §5, §6, §9, §13; supersedes nothing.
**Date**: 2026-07-22
**Reason**: v1 shipped with segmentation only at ingest and per-user cadence limited to a global 1-send/24h throttle. For aged-record remarketing, over-mailing burns per-ISP reputation; under-powered segmentation blocks backfill and re-engagement campaigns.

---

## 1. Frequency caps + quiet hours

### 1.1 Caps (per recipient, across ALL workflows and campaigns)

New Settings (env-configurable, single-tenant v1):

```python
frequency_cap_per_week: int = 3        # rolling 7 days
frequency_cap_per_month: int = 8       # rolling 30 days
send_window_start_hour: int = 9        # recipient-local time, inclusive
send_window_end_hour: int = 20         # recipient-local time, exclusive
send_default_timezone: str = "America/Chicago"
```

Enforcement in the send worker, same pattern as the existing 24h throttle: one
batched query per cycle counting sends in the rolling windows per claimed
recipient; over-cap sends are re-queued with `queued_at` pushed to the earliest
timestamp the cap frees up. Order of send-worker gates becomes: throttle-24h →
weekly cap → monthly cap → quiet hours → suppression → quota → rate limit.

### 1.2 Quiet hours / timezone

- Recipient timezone derived from `attributes.state` via a pure domain map
  (`app/domain/timezone.py`, US state → IANA zone; split-zone states use the
  dominant zone). Missing/unknown state → `send_default_timezone`.
- A send whose dispatch moment falls outside [start_hour, end_hour) in the
  recipient's local time is re-queued to the next window opening.
- Applies to workflow sends AND manual campaign sends. No DB change needed.

## 2. Engagement-decay back-off

### 2.1 Tiers

Nightly worker (`engagement_scorer`) classifies every active recipient:

| Tier | Definition (defaults, configurable) |
|---|---|
| `engaged` | open/click within last 30 days |
| `neutral` | no engagement in 30 days, < 5 unopened sends in a row |
| `cold` | ≥ 5 consecutive sends with zero engagement |
| `dormant` | ≥ 10 consecutive sends with zero engagement |

New columns on `recipients` (one migration):

```sql
engagement_tier     text NOT NULL DEFAULT 'neutral',
tier_computed_at    timestamptz
```

### 2.2 Effect (enforced in send worker, batched)

| Tier | Minimum gap between sends |
|---|---|
| `engaged` | 24h (existing throttle) |
| `neutral` | 72h |
| `cold` | 7 days |
| `dormant` | auto-suppress: insert into `suppressions` with `source='engagement_decay'`, exit active journeys on next tick |

Pure tier/gap logic lives in `app/domain/engagement.py`; the worker and send
worker consume it. Dormant auto-suppression is reversible (delete the
suppression row) and is pushed to OTTO via the existing unsub outbox with a
distinct reason so OTTO can distinguish it from a real unsubscribe.

## 3. Segment-driven sends

### 3.1 DSL v2 (extends §6.1 `entry_segment`, stays JSON)

```jsonc
{ "all": [                                 // AND; "any": [...] for OR
    { "field": "vertical", "eq": "auto" },
    { "field": "attributes.state", "in": ["TX", "OK"] },
    { "field": "attributes.age_band", "not_in": ["18-24"] },
    { "field": "last_seen_at", "before": "P60D" },   // relative-to-now durations
    { "field": "engagement_tier", "eq": "neutral" },
    { "kind": "no_engagement_since", "window": "P60D" }
  ] }
```

Old flat form (`{"vertical": "auto"}`) remains valid — `app/domain/segment.py`
normalizes it to DSL v2. Evaluation gains a second implementation that compiles
the DSL to SQLAlchemy predicates (JSONB `->>` on attributes) so segments run
as ONE query against the recipient base, not per-row Python.

### 3.2 New API

- `POST /api/segments/preview` — body: segment JSON → `{count, sample: [10 recipients]}`
- `POST /api/workflows/{id}/backfill` — body: segment JSON + `limit` →
  enrolls matching existing recipients as journeys. Respects re-entry cooldown,
  active-journey uniqueness, and suppression. Returns enrolled/skipped counts.
- `POST /api/campaigns/{id}/send` — accepts `{"segment": {...}}` as a third
  alternative to `recipient_ids` / `email_list`. Resolved server-side with the
  same 10K cap.

## 4. Explicitly out of scope (v2+)

- Per-recipient preferred-hour learning (send-time optimization — SPEC §19)
- Segment builder UI (JSON only; SPEC §19)
- Per-tenant cap overrides (single-tenant v1 → Settings is enough)

## 5. SPEC sections to update on merge

- §5 recipients table: add `engagement_tier`, `tier_computed_at`
- §6.1: entry_segment DSL v2 grammar
- §9.2: unsub outbox carries `engagement_decay` reason
- §13: add caps + quiet hours to the send worker gate list
- §17: note post-M7 addendum-002 work
