# Cadence ESP — Operations Runbook

## 1. Purpose and Scope

This runbook is for on-call engineers operating the Cadence email service. It covers diagnosis and mitigation of production incidents, routine operational tasks, and daily/weekly health checks. Cadence sends transactional/remarketing email for OTTO auto insurance via AWS SES (Managed Dedicated IPs). It is not an architecture overview — skip to the relevant scenario when something is broken.

---

## 2. Quick Reference

### Logs

```bash
# API process
docker compose logs -f api

# Background worker process (send, tick, stats, otto sync)
docker compose logs -f worker

# All services together
docker compose logs -f
```

Logs are structured JSON (structlog). Every log line that touches a send includes `journey_id`, `recipient_id`, `campaign_id`, `send_id`. Filter on those fields to trace a single send end-to-end.

### Metrics

- Prometheus endpoint: `GET /metrics` (scraped internally; not public-facing through Caddy)
- Application dashboard: navigate to `/dashboard` in the browser (served by Caddy on port 80/443)

Key gauges to check at a glance:
- `esp_sends_total{status}` — sends by outcome
- `esp_workflow_journeys_active` — active journey count
- `esp_workflow_tick_duration_seconds` — tick latency
- `esp_otto_sync_failures_total` — unsub sync failures

### Health check

```bash
curl https://<host>/health
# Expected: {"status": "ok"}
```

A non-200 or wrong body means the API container is down or has lost its database connection.

### Database access

```bash
psql "$CADENCE_DATABASE_URL"
```

The DSN is the same value set in the `.env` file under `CADENCE_DATABASE_URL`. Do not commit `.env` to the repo.

### Emergency send stop

**Option A — pause all active workflows (preferred).** This stops the workflow tick from advancing journeys and therefore stops new sends from being queued. Sends already in `status='queued'` will still drain unless you also stop the worker container.

```sql
BEGIN;
UPDATE workflows SET status = 'paused', updated_at = now()
WHERE status = 'active';
-- Verify row count before committing.
COMMIT;
```

Reverse:

```sql
BEGIN;
UPDATE workflows SET status = 'active', updated_at = now()
WHERE status = 'paused';
COMMIT;
```

**Option B — stop the worker container.** Prevents all sending without touching DB state. Journeys remain `active` and will resume when the worker restarts.

```bash
docker compose stop worker
# Restart when ready:
docker compose start worker
```

**Option C — stop SES sending at the AWS account level.** Use the AWS console or CLI to disable sending. This is a last resort — SES may take a few minutes to propagate. Do this only if you need to stop sends regardless of Cadence state (e.g., account suspended scenario).

---

## 3. Routine Operations

### Deploy a new version

```bash
# Pull the new image (or rebuild locally)
docker compose build

# Rotate containers with zero-downtime (migrate → api → worker)
docker compose up -d migrate
# Wait for migrate to exit successfully, then:
docker compose up -d api worker caddy static-init
```

The `migrate` service runs `alembic upgrade head` and exits. The `api` and `worker` services depend on `migrate` completing successfully (enforced by `depends_on: condition: service_completed_successfully` in `docker-compose.yml`).

Monitor logs during cutover:

```bash
docker compose logs -f api worker
```

### Roll back

```bash
# Step 1: roll back the schema (if the migration introduced schema changes)
docker compose run --rm migrate uv run alembic downgrade -1

# Step 2: bring up the previous image tag
# Edit docker-compose.yml to pin the previous image, then:
docker compose up -d api worker
```

If the migration cannot be cleanly reversed, restore from Aurora snapshot before proceeding. Do not attempt a code rollback without also reverting the schema.

### Run a migration manually

```bash
# Upgrade to latest
docker compose run --rm migrate uv run alembic upgrade head

# Generate a new revision (run on dev, commit the file, do not run autogenerate in prod)
uv run alembic revision --autogenerate -m "description"

# Inspect current revision
docker compose exec api uv run alembic current
```

### Add a suppression by hand

```bash
psql "$CADENCE_DATABASE_URL" <<'SQL'
INSERT INTO suppressions (id, tenant, email, reason, source, suppressed_at)
VALUES (gen_random_uuid(), 'otto', 'user@example.com', 'manual', 'admin_user', now())
ON CONFLICT (tenant, email) DO NOTHING;
SQL
```

Or via the API:

```bash
curl -s -X POST https://<host>/api/suppressions \
  -H "X-API-Key: $CADENCE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant":"otto","email":"user@example.com","reason":"manual","source":"admin_user"}'
```

### Remove a suppression by hand

```bash
curl -s -X DELETE "https://<host>/api/suppressions/user@example.com?tenant=otto" \
  -H "X-API-Key: $CADENCE_API_KEY"
```

Or directly:

```bash
psql "$CADENCE_DATABASE_URL" <<'SQL'
DELETE FROM suppressions WHERE tenant = 'otto' AND email = 'user@example.com';
-- Also restore the recipient status if applicable:
UPDATE recipients SET status = 'active', updated_at = now()
WHERE tenant = 'otto' AND email = 'user@example.com';
SQL
```

---

## 4. Incident Scenarios

---

### 4.1 SES Bounce Rate > 5% (active alert)

**Symptom.** Slack alert: `[CRITICAL] Alert ACTIVATED | metric=bounce_rate value=... threshold=0.05`. Dashboard bounce rate card is red.

**Impact.** AWS will warn first, then place sending restrictions or suspend the account if the rate stays elevated.

**Diagnosis.**

```sql
-- Which campaigns are driving bounces?
SELECT campaign_id, count(*) AS bounces
FROM events
WHERE event_type = 'bounce' AND occurred_at >= now() - interval '2 hours'
GROUP BY campaign_id ORDER BY bounces DESC LIMIT 20;

-- Are bounces hard or soft?
SELECT event_subtype, count(*)
FROM events
WHERE event_type = 'bounce' AND occurred_at >= now() - interval '2 hours'
GROUP BY event_subtype;
```

Hard bounces from stale/invalid addresses are the most common cause. Check whether a specific cohort or aged-lead batch was just ingested.

**Mitigation.**

1. Pause affected campaigns or the entire workflow (see §2 emergency stop).
2. If a specific batch of bad addresses caused the spike, suppress them:

```sql
-- Suppress all hard-bounced addresses that are still active
UPDATE recipients SET status = 'bounced_hard', updated_at = now()
WHERE tenant = 'otto'
  AND status = 'active'
  AND id IN (
    SELECT s.recipient_id FROM sends s
    JOIN events e ON e.send_id = s.id
    WHERE e.event_type = 'bounce' AND e.event_subtype = 'hard'
      AND e.occurred_at >= now() - interval '24 hours'
  );
```

3. Hard bounces from `POST /webhooks/ses` processing should already auto-suppress. Confirm suppression rows were written:

```sql
SELECT count(*) FROM suppressions WHERE reason = 'hard_bounce' AND suppressed_at >= now() - interval '2 hours';
```

4. Open the AWS SES console. Check the account-level reputation dashboard. If a formal SES warning email was received, respond via AWS Support before resuming.

**Postmortem notes.** Root cause is usually a list-quality problem (unverified or purchased emails) or sending to a segment that was dormant too long. Implement a re-engagement gate in the workflow before re-activating.

---

### 4.2 SES Complaint Rate > 0.1% (active alert)

**Symptom.** Slack alert: `[CRITICAL] Alert ACTIVATED | metric=complaint_rate`. Complaints appear in the `events` table with `event_type = 'complaint'`.

**Impact.** Complaint rates above 0.1% trigger AWS enforcement action. Gmail and Yahoo route messages to spam, degrading deliverability.

**Diagnosis.**

```sql
-- Which campaigns generated complaints?
SELECT campaign_id, variant_label, count(*) AS complaints
FROM events
WHERE event_type = 'complaint' AND occurred_at >= now() - interval '24 hours'
GROUP BY campaign_id, variant_label ORDER BY complaints DESC;
```

Check whether the variant content is misleading or the subject line is too aggressive. Check whether the `from` address or domain changed recently.

**Mitigation.**

1. Pause or retire the offending variants via the UI (Campaigns > variant row > Retire), or directly:

```sql
UPDATE templates SET status = 'retired', updated_at = now()
WHERE campaign_id = '<uuid>' AND variant_label = 'B';
```

2. All complaint addresses are auto-suppressed via webhook processing. Verify:

```sql
SELECT count(*) FROM suppressions WHERE reason = 'complaint' AND suppressed_at >= now() - interval '2 hours';
```

3. If the complaint rate does not drop within 30 minutes after pausing, escalate to §5.

**Postmortem notes.** Review copy for misleading claims. Ensure List-Unsubscribe headers are rendering correctly — if users cannot find the unsub link they resort to spam buttons. Confirm `POST /u/{token}` is reachable without authentication (test with `curl -X POST`).

---

### 4.3 SES Account Suspended

**Symptom.** All sends fail with `MessageRejected` or `AccountSuspended` from boto3. AWS may email the account root. CloudWatch `Managed.SendingAllowed` metric = 0. Dashboard SES reputation panel shows sending disabled.

**Impact.** No emails leave. All journeys stall at the next `send` node.

**Diagnosis.**

```bash
# Check via AWS CLI
aws sesv2 get-account --region us-east-1 | python3 -m json.tool | grep -i sending
```

Or check for `SendingEnabled: false` / a non-null `EnforcementStatus` in the response.

**Mitigation.**

1. Stop the worker to prevent log noise: `docker compose stop worker`.
2. Log into AWS Support and open an emergency case. Reference the enforcement notification email.
3. While awaiting reinstatement: scrub your list. Identify and bulk-suppress the addresses that caused the boundary breach.
4. Do not restart the worker until AWS confirms sending is re-enabled.
5. Once reinstated, resume with a reduced rate (`CADENCE_SEND_RATE_LIMIT_PER_SECOND` lowered) and monitor for 24 hours before returning to full rate.

**Postmortem notes.** Request production access early in any new environment setup — sandbox mode (200/day, only verified addresses) masks list-quality problems. This is the worst-case outcome of unresolved bounce/complaint alerts.

---

### 4.4 Aurora DB Outage

**Symptom.** API returns 500 on all endpoints. Worker logs show repeated SQLAlchemy connection errors or `asyncpg` exceptions. `GET /health` returns 500 or hangs.

**Impact.** No sends, no journey advancement, no webhook processing. SNS will retry webhook delivery for up to 23 days — events are not lost, just delayed.

**Diagnosis.**

```bash
docker compose logs api | tail -50
docker compose logs worker | tail -50
```

Check Aurora status in the AWS RDS console. Check if a failover is in progress (Aurora Multi-AZ failover takes ~30 seconds).

**Mitigation.**

1. If failover in progress: wait. Containers will reconnect automatically once the writer endpoint resolves. No action needed if the outage is short.
2. If the DB is genuinely unavailable for more than 5 minutes: stop the worker to avoid crash loops.

```bash
docker compose stop worker
```

3. Once the DB recovers: `docker compose start worker`. Check that the workflow tick resumes processing.
4. Confirm no journeys are stuck in `errored` status due to mid-transaction failures:

```sql
SELECT status, count(*) FROM journeys GROUP BY status;
-- Investigate errored journeys:
SELECT id, workflow_id, recipient_id, current_node_id
FROM journeys WHERE status = 'errored' LIMIT 20;
```

5. Queued sends that didn't go out will drain normally once the worker restarts — they remain in `status='queued'`.

**Postmortem notes.** Ensure Aurora Multi-AZ is enabled. Check whether the connection pool settings in `app/core/db.py` need tuning for faster reconnect.

---

### 4.5 Send Worker Queue Backed Up

**Symptom.** Growing count of sends in `status='queued'`. Journeys are advancing (tick is healthy) but emails are not leaving. Dashboard delivery rate drops.

**Impact.** Sends are delayed. Recipients in time-sensitive workflows receive emails hours or days late.

**Diagnosis.**

```sql
-- How many sends are queued and how old is the oldest?
SELECT count(*), min(queued_at) AS oldest
FROM sends WHERE status = 'queued';
```

```bash
# Is the worker running?
docker compose ps worker
docker compose logs worker | tail -30
```

Common causes:
- Worker container exited or is crash-looping (check logs).
- SES rate-limit errors: sends are failing and retrying (`status='failed'` with `failure_reason` like `Throttling`).
- SES account quota exhausted for the day (`send_daily_quota` setting; default 50,000/day).

**Mitigation.**

1. If the worker is not running: `docker compose start worker`.
2. If SES is throttling: lower `CADENCE_SEND_RATE_LIMIT_PER_SECOND` in `.env` and restart the worker. The token bucket in `app/workers/send_worker.py` enforces the rate; no code change needed.
3. If daily quota is exhausted, sends resume automatically after midnight UTC (the quota resets daily at the SES account level). Do not manually requeue — sends will drain the following day.
4. If sends are failing for another reason, inspect `failure_reason`:

```sql
SELECT failure_reason, count(*) FROM sends
WHERE status = 'failed' AND queued_at >= now() - interval '2 hours'
GROUP BY failure_reason ORDER BY count DESC;
```

**Postmortem notes.** Request a daily quota increase from AWS before volume exceeds the current cap. Monitor `esp_sends_total{status="failed"}` Prometheus counter.

---

### 4.6 Workflow Tick Worker Stalled

**Symptom.** Journeys are stuck — `next_action_at` is in the past but `status` stays `active`. No sends being queued from workflow nodes. Prometheus `esp_workflow_tick_duration_seconds` stops updating. Alert fires if tick lag > 60s.

**Impact.** The entire nurture sequence is frozen. No emails advance to the send queue.

**Diagnosis.**

```sql
-- How many active journeys are overdue?
SELECT count(*) FROM journeys
WHERE status = 'active' AND next_action_at < now() - interval '5 minutes';
```

```bash
docker compose logs worker | grep workflow_tick | tail -20
```

The tick worker runs every 10 seconds (`workflow_tick_interval_seconds`). If the log shows no `workflow_tick.advanced` or `workflow_tick.error` entries for > 30 seconds, the scheduler is stalled.

Common causes:
- DB connection issue (see §4.4).
- A journey is holding a `FOR UPDATE SKIP LOCKED` lock and blocking — unlikely but check for long-running transactions.
- Worker process OOM-killed.

**Mitigation.**

1. Restart the worker: `docker compose restart worker`.
2. If the worker restarts cleanly, the tick will resume and process the backlog (up to 1000 journeys per tick, ordered by `next_action_at`).
3. Check for errored journeys that may have put the worker into a tight crash loop:

```sql
SELECT id, current_node_id, context FROM journeys
WHERE status = 'errored' AND updated_at >= now() - interval '1 hour'
ORDER BY updated_at DESC LIMIT 10;
```

Errored journeys do not block the tick — each is caught individually. If many journeys errored on the same node, there is likely a data problem in that node's campaign or template.

**Postmortem notes.** `app/workers/workflow_tick.py` uses `SKIP LOCKED` so a single bad journey will not block others. If a journey keeps erroring, inspect `journeys.context` for clues and manually set it to `exited` if it cannot recover.

---

### 4.7 Anthropic API Errors During AI Generation

**Symptom.** Campaign stays in `status='generating'` for more than 30 minutes. Worker logs show `ai_generator.generate_failed` entries. Alert fires if AI generation failures > 5/hr.

**Impact.** No new variants are created for the affected campaign. Existing approved variants continue sending normally — this only blocks campaigns waiting on their first generation run.

**Diagnosis.**

```bash
docker compose logs worker | grep ai_generator | tail -30
```

Look for `error` fields — common causes: API key invalid, rate limit, model unavailable, malformed response.

```sql
-- Which campaigns are stuck generating?
SELECT id, name, status, updated_at FROM campaigns WHERE status = 'generating';
```

**Mitigation.**

1. Check `CADENCE_ANTHROPIC_API_KEY` is set correctly in `.env`.
2. Check [https://status.anthropic.com](https://status.anthropic.com) for an outage.
3. If the campaign is stuck but the underlying error is transient, reset it so generation can be retried:

```sql
UPDATE campaigns SET status = 'draft', updated_at = now()
WHERE status = 'generating';
```

Then re-trigger generation via the UI (Campaign detail > Generate Variants) or via the API.

4. If a specific cohort is causing failures (malformed `attributes` data), the generation loop will skip it and log the error — other cohorts still generate. The campaign will still reach `ready` status after all other cohorts complete.

**Postmortem notes.** The generation worker in `app/workers/ai_generator.py` catches per-cohort exceptions and continues. A campaign with partial errors reaches `ready` with fewer variants than expected. Check `templates` count vs expected cohort count after generation completes.

---

### 4.8 OTTO API Down

Two sub-scenarios share the same root cause (OTTO API unavailable) but affect different data flows.

**A. Lead ingestion failing**

**Symptom.** No new recipients appearing. `POST /api/ingest/recipients` may return errors if the caller depends on OTTO data being available, or the caller (OTTO) is not calling us at all. Check ingest API logs.

**Impact.** New leads are not entering workflows. Existing journeys are unaffected.

**Mitigation.** The ingest endpoint is our API that OTTO calls — we do not call OTTO to ingest leads. If OTTO's system is down, they simply are not pushing leads to us. No action required on our side. When OTTO recovers, they can push a backfill batch; ingestion is idempotent on `(tenant, email)` so duplicate pushes are safe.

**B. Unsubscribe sync failing (outbox not draining)**

**Symptom.** `unsub_outbox` rows accumulating with `delivered_at IS NULL` and incrementing `attempts`. Alert fires when > 100 undelivered rows after 1 hour. Worker logs show `otto_sync.push_failed`.

**Impact.** Unsubscribes are held locally and not reflected in OTTO's system. Recipients are suppressed in Cadence immediately — no further email goes out. The data gap is in OTTO's side only.

**Diagnosis.**

```sql
SELECT count(*), max(attempts), min(created_at) AS oldest
FROM unsub_outbox WHERE delivered_at IS NULL;
```

```bash
docker compose logs worker | grep otto_sync | tail -20
```

**Mitigation.**

1. The outbox worker retries automatically with exponential backoff (1m, 5m, 30m, 2h, 6h). Max 10 attempts (`app/workers/otto_sync.py`).
2. When OTTO recovers, the worker will drain the backlog on the next scheduled run (60-second interval).
3. If rows have hit 10 attempts and are stuck:

```sql
-- Reset attempts to allow retry
UPDATE unsub_outbox SET attempts = 0, last_error = NULL
WHERE delivered_at IS NULL AND attempts >= 10;
```

4. If OTTO has a known extended outage, coordinate with OTTO team to provide a bulk unsub list directly when they come back online.

**Postmortem notes.** Set `CADENCE_OTTO_UNSUB_ENDPOINT` and `CADENCE_OTTO_API_KEY` correctly in `.env`. The endpoint contract is **TODO:** (confirm with OTTO team — see SPEC §18 open item 1).

---

### 4.9 Webhook Deliverability Issues

**Symptom.** Sends show `status='sent'` but never progress to `delivered`, `bounced`, or `complained`. Event counts in the dashboard go cold even though sends are going out. `events` table receives no new rows for > 30 minutes despite active sending.

**Impact.** MAB stats do not update. Bounce/complaint suppression does not fire. Reputation alerts cannot trigger.

**Diagnosis.**

1. Check that the SNS topic is still subscribed to `POST /webhooks/ses`:

In the AWS SNS console, navigate to the topic specified by `CADENCE_SNS_TOPIC_ARN_SES_EVENTS`. Confirm the HTTPS subscription to your domain is `Confirmed`.

2. Confirm the endpoint is reachable:

```bash
curl -s -o /dev/null -w "%{http_code}" https://<host>/webhooks/ses -X POST -d '{}'
# Expected: 200
```

3. Check for recent webhook processing in logs:

```bash
docker compose logs api | grep webhooks | tail -30
```

If no `webhook.ses` entries appear for > 30 minutes during active sending, SNS is not delivering.

4. Check whether `sns_verify_signatures` is causing rejections:

```bash
docker compose logs api | grep "webhook.parse_failed" | tail -10
```

**Mitigation.**

1. If the SNS subscription is unconfirmed: re-subscribe the HTTPS endpoint in the SNS console. The webhook handler at `app/api/webhooks.py` auto-confirms `SubscriptionConfirmation` messages.
2. If the SNS subscription is confirmed but deliveries are failing: check SNS delivery logs in CloudWatch (`SNS/NumberOfNotificationsFailed`).
3. If Caddy is the problem (TLS certificate expired, routing misconfigured): check `docker compose logs caddy`.
4. If signature verification is incorrectly rejecting valid payloads (misconfigured or test environment): temporarily set `CADENCE_SNS_VERIFY_SIGNATURES=false` in `.env` and restart the API to confirm. Restore to `true` once resolved.

**Postmortem notes.** Bounces and complaints that arrived during the outage will not be retroactively processed. After restoring the webhook, manually check whether any hard bounces or complaints occurred in SES CloudWatch during the gap and suppress those addresses manually.

---

### 4.10 Mass Unsubscribe Spike

**Symptom.** Unsub rate for a campaign or time window spikes sharply. `events` table has a large volume of `event_type = 'unsubscribe'` in a short window. `unsub_outbox` queue grows rapidly.

**Impact.** May indicate a deliverability or brand trust problem. If spoofed (invalid token attacks), suppression rows may be written for addresses that never requested it.

**Diagnosis.**

```sql
-- How many unsubs in the last hour?
SELECT count(*) FROM events
WHERE event_type = 'unsubscribe' AND occurred_at >= now() - interval '1 hour';

-- Are they coming through valid signed tokens or the SNS webhook?
SELECT source, count(*) FROM suppressions
WHERE reason = 'unsubscribe' AND suppressed_at >= now() - interval '1 hour'
GROUP BY source;
```

Unsubs via `source = 'one_click'` came through the signed URL endpoint (`app/api/unsub.py`). The token is verified with HMAC — spoofing is not possible without the `unsub_signing_key`. A spike here is a real brand signal.

Unsubs via `source = 'sns'` came through the SES feedback loop — genuine mailbox provider unsubscribes.

**Mitigation.**

1. If the spike is genuine (real user unsubs): pause the campaign, review copy for triggering content, do not re-suppress without reviewing.
2. If you suspect token replay or URL scraping (same token appears multiple times): the `one_click` handler in `app/api/unsub.py` is idempotent — it will only suppress once. Repeated POSTs are safe.
3. If you need to remove a wrongly-suppressed address: see §3 (remove suppression by hand). Also clear `recipients.status` back to `active`.
4. Notify the product team if unsub rate exceeds 0.5% in a 1-hour window — this may indicate a content or targeting problem that requires a campaign pause.

**Postmortem notes.** A real unsub spike after a new campaign launch is an early signal the content is off-brand. Retire the problematic variants before the complaint rate follows.

---

### 4.11 Merge-Tag Rendering Failure

**Symptom.** Recipients receive emails with literal `{{firstName}}` or `{{city}}` text visible. Or sends fail in bulk with `failure_reason = 'missing merge tag: <tag>'` in the `sends` table.

**Impact.** Customer-facing: poor recipient experience and brand damage. Data: sends marked `status='failed'` do not count toward SES quota but are lost revenue opportunities.

**Diagnosis.**

```sql
-- Find sends that failed due to missing merge tags
SELECT s.id, s.recipient_id, s.campaign_id, s.failure_reason
FROM sends s
WHERE s.status = 'failed'
  AND s.failure_reason LIKE 'missing merge tag:%'
  AND s.queued_at >= now() - interval '2 hours'
LIMIT 20;

-- Which merge tag is missing?
SELECT failure_reason, count(*) FROM sends
WHERE status = 'failed' AND failure_reason LIKE 'missing merge tag:%'
GROUP BY failure_reason ORDER BY count DESC;
```

The send worker (`app/workers/send_worker.py`) catches `MissingMergeTag` and marks the send failed — literal tags cannot ship.

If emails did go out with literal tags: the template was generated without the tag (possible if the AI output a static string instead of a merge tag, or a template was manually edited). Check `templates.body_html` directly.

**Mitigation.**

1. If sends are failing (not going out): the guard worked. Fix the template by editing the body or re-generating the AI variant.

```sql
-- Inspect the template body
SELECT subject, body_html FROM templates WHERE id = '<template_id>';
```

2. If literal tags shipped: pause the campaign immediately. Check whether the tag is missing from `recipients.attributes` for the affected cohort, or the tag name in the template is misspelled.

3. Re-queue failed sends (only after fixing the underlying template or recipient data):

```sql
-- Reset to queued so the worker retries
UPDATE sends SET status = 'queued', failure_reason = NULL
WHERE status = 'failed'
  AND failure_reason LIKE 'missing merge tag: first_name%'
  AND campaign_id = '<uuid>';
```

4. Use the template preview screen in the UI (Template > Preview with mock data) to catch missing tags before a campaign goes live.

**Postmortem notes.** The `render` function in `app/domain/rendering.py` raises `MissingMergeTag` on any unresolvable tag with no default. Templates that use Jinja2 `| default(...)` filters will not trigger this. Enforce preview sign-off in the campaign approval workflow.

---

## 5. Escalation

**TODO:** Fill in oncall rotation details, Slack channel, and PagerDuty key.

| Situation | Owner | Contact |
|---|---|---|
| SES account suspended or enforcement action | **TODO:** ESP / infrastructure owner | **TODO:** |
| Aurora DB unavailable > 15 minutes | **TODO:** Infrastructure team | **TODO:** |
| Anthropic API outage (AI generation blocked) | **TODO:** Product / engineering lead | **TODO:** |
| OTTO API down affecting unsub sync | **TODO:** OTTO integration owner | **TODO:** |
| Complaint/bounce rate breach (escalated) | **TODO:** Product team | **TODO:** |
| Code bug in send or tick worker | **TODO:** On-call engineer | **TODO:** |

**Slack channel:** **TODO:** `#cadence-oncall` or equivalent.

**PagerDuty service key:** **TODO:**

### When to page which team

- **SES ops team:** account suspended, enforcement action received from AWS, Managed IP pool showing unhealthy (`Managed.SendingAllowed = 0`).
- **Infrastructure team:** Aurora unavailable > 15 minutes, EC2 host unreachable, disk full, OOM kills.
- **Product team:** complaint rate sustained breach, mass unsubscribe spike, OTTO data quality issue causing bulk bad-address bounces.

---

## 6. On-Call Checklist

### Daily

- [ ] Check dashboard for active alerts (bounce/complaint rate cards).
- [ ] Check SES reputation in the AWS console: navigate to SES > Account dashboard > Sending statistics. Confirm bounce rate < 5%, complaint rate < 0.1%.
- [ ] Confirm at least one send occurred in the last 24 hours (dead-air detection):

```sql
SELECT max(sent_at) AS last_send FROM sends WHERE status = 'sent';
```

- [ ] Check `unsub_outbox` drain:

```sql
SELECT count(*) AS pending, max(attempts) AS max_attempts
FROM unsub_outbox WHERE delivered_at IS NULL;
```

Pending count should be near zero (drains within 60 seconds normally). A stuck queue with high `max_attempts` needs investigation (see §4.8).

### Weekly

- [ ] Check Aurora CPU, connection count, and free storage in the RDS console. Alert if storage headroom < 20%.
- [ ] Check `events` partition coverage: confirm the current month's partition exists and next month's is pre-created.

```sql
SELECT relname FROM pg_class
WHERE relname LIKE 'events_%' ORDER BY relname;
```

- [ ] Review variant stats for any campaigns in `status='generating'` for > 1 hour (stuck AI jobs).
- [ ] Check `journeys` for a large count of `status='errored'`. Investigate if count > 50.

```sql
SELECT status, count(*) FROM journeys GROUP BY status;
```

- [ ] Verify the SES Managed IP pool is healthy in CloudWatch (`Managed.SendingAllowed`, `Managed.DedicatedIpCount`).

---

## 7. Known Gotchas

### SES sandbox vs. production

New SES accounts start in sandbox mode: 200 sends/day, only to verified email addresses. The system is built assuming production access. In dev or staging, point `CADENCE_ESP_BACKEND=noop` or use sandbox mode with only verified test addresses. Request production access (`Moving out of the SES sandbox`) via AWS Support before any real list sending. Do not test against a live OTTO list while in sandbox — most sends will silently fail.

### SES messageId vs. SNS messageId

There are two different message IDs in play when an SNS notification arrives at `POST /webhooks/ses`. The SNS envelope has its own `MessageId`. The SES event inside the notification body has a separate `messageId` field. The `sends` table stores the SES messageId as `esp_message_id` — this is what joins events to sends in `app/api/webhooks.py`. Do not confuse the two when debugging missing event correlation.

### List-Unsubscribe-Post must actually work

Gmail and Yahoo require RFC 8058 one-click unsubscribe for senders above 5,000/day. Setting the `List-Unsubscribe-Post` header is not sufficient — the `POST /u/{token}` endpoint must accept unauthenticated POST requests and unsubscribe immediately. The header is set in `app/workers/send_worker.py` and the endpoint is in `app/api/unsub.py`. Test this end-to-end: `curl -X POST https://<host>/u/<token>` must return 200 and write a suppression row, with no authentication required.

### Workflow version pinning

`journeys.workflow_version` is set at the time the journey is created. The workflow tick currently uses the live `workflow.definition` regardless of this version (see the TODO comment in `app/workers/workflow_tick.py`). This means editing a workflow definition mid-flight affects in-progress journeys. Until version snapshotting is implemented, treat active workflow definitions as immutable — create a new workflow version and let in-flight journeys complete on the old one before activating changes.

### Idempotent lead ingestion

`POST /api/ingest/recipients` is idempotent on `(tenant, email)`. Re-ingesting the same lead does not create duplicate `recipients` rows and does not create a second journey if the recipient is already active in a workflow (enforced by the `UNIQUE (workflow_id, recipient_id)` constraint on `journeys`). Do not write custom code to deduplicate before calling ingest — the DB constraint is the guard.

### REFRESH MATERIALIZED VIEW CONCURRENTLY requires a unique index

`variant_stats` is refreshed every 5 minutes by `app/workers/stats_rollup.py` using `REFRESH MATERIALIZED VIEW CONCURRENTLY`. This requires the unique index `CREATE UNIQUE INDEX ON variant_stats (campaign_id, cohort_key, variant_label)`. Do not drop this index. If a migration accidentally removes it, the stats rollup worker will fail with a Postgres error and MAB will run on stale data.
