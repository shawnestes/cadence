# SPEC Addendum 001 — SES Dedicated IPs (Managed)

**Status**: Supersedes portions of SPEC §13 (Send Rate Limiting & SES Quotas).
**Date**: 2026-04-21
**Reason**: After spec draft, confirmed SES offers two dedicated-IP models. Managed is the better fit for this system's volume profile (100K–1M/mo) and bursty sending pattern (aged-record re-engagement blasts).

---

## 1. Decision

Use **SES Dedicated IPs (Managed)** instead of Dedicated IPs (Standard).

## 2. What changes vs. original spec

### 2.1 Warmup is no longer our problem

Original SPEC §13 specified a manual warmup schedule (`warmup_schedule` config, send worker enforcing daily caps during ramp). **Delete this.** SES Managed handles warmup adaptively per-ISP:

- Tracks warmup state independently for Gmail, Yahoo, Hotmail/Outlook, Apple, etc.
- Spills excess sending over to the SES shared pool during ramp — no send is lost, it just may go via shared until the dedicated pool can absorb it.
- When sending volume to a given ISP drops, the per-ISP warmup % decays — this matters for remarketing because your traffic is uneven.
- Auto-scales the number of IPs in the pool up and down based on sending patterns.

### 2.2 No warmup enforcement in send worker

The send worker still enforces:
- Per-second rate limit (from `send_rate_limit_per_second` config)
- Per-day account quota tracking (to avoid hitting SES account cap)
- Per-recipient throttling rules (e.g., max 1 send per recipient per 24h)

The send worker **no longer** enforces:
- ~~Per-day warmup caps~~
- ~~Per-IP volume distribution~~
- ~~IP selection logic~~

### 2.3 Configuration changes

Remove from `Settings`:
- `warmup_schedule` (was planned)

Add to `Settings`:
```python
ses_managed_pool_name: str      # the managed IP pool name to attach to the config set
```

The configuration set (`ses_configuration_set`) is associated with the managed pool once at infra setup time; no runtime pool selection needed.

### 2.4 Reputation monitoring

Managed IPs expose **per-ISP reputation** via CloudWatch metrics. This is a material upgrade over what the spec assumed. Update the deliverability dashboard (SPEC §11.2) to include:

- Per-ISP reputation gauges (Gmail, Yahoo, Outlook, Apple iCloud as minimum tiles)
- Per-ISP delivery rate from our own `events` data, joined against the ISP reputation signal
- Effective daily rate per top ISP from CloudWatch

New CloudWatch metrics to pull into the reputation monitor worker:
- `Reputation.IspFeedback` (per ISP)
- `Managed.DedicatedIpCount` — number of IPs currently allocated to the managed pool (useful as an ops signal: if this scales up, volume is climbing)
- `Managed.SendingAllowed` — whether managed pool is healthy

### 2.5 Cost model

Managed is **volume-priced** rather than per-IP/month. At your projected scale:
- 100K/mo → very low managed cost, Standard's fixed IP rental is dead weight
- 1M/mo → pricing is competitive with Standard, and you avoid the overhead of deciding how many IPs to rent
- Verify current pricing at https://aws.amazon.com/ses/pricing/ before launch — this can change

### 2.6 What stays the same

- Everything about configuration sets, event destinations, SNS webhook subscription, DKIM/SPF/DMARC setup
- The ESP adapter interface is unchanged — `SESAdapter.send()` doesn't care whether the underlying pool is Managed or Standard
- Sandbox-to-production access request process is unchanged
- Account-level bounce/complaint thresholds still apply (5% bounce, 0.1% complaint)

## 3. What to do at project kickoff (infra setup)

1. Request SES production access (sandbox → prod) — unchanged from original plan.
2. Verify sending domain, set up DKIM/SPF/DMARC DNS records.
3. **Create a managed IP pool** in the SES console. No walkthrough to allocate specific IPs — SES provisions as needed based on first sends.
4. Create the configuration set. Associate it with the managed pool.
5. Configure event destination → SNS topic → HTTPS subscription to `POST /webhooks/ses`.
6. Do *not* request specific dedicated IPs via AWS Support — that's the Standard path.

## 4. Re-evaluation triggers

Revisit Standard vs. Managed if any of these become true:
- You need a specific IP allowlisted by a partner (rare in B2C remarketing; relevant for B2B)
- You want an IP that never spills into shared (compliance/enterprise reasons)
- Sustained volume exceeds ~5M/mo and per-IP cost arithmetic starts favoring Standard
- You add a second tenant (e.g., Giraffe in v2) and want strict IP isolation between tenants

## 5. Spec sections to update

When merging this addendum into SPEC.md:

- §2 "Tech Stack": change `AWS SES (boto3)` row note to "SES with Managed Dedicated IPs"
- §8.2 "SES adapter": add note that `get_reputation()` pulls per-ISP metrics from CloudWatch for the managed pool
- §11.2 "Deliverability dashboard": add per-ISP reputation section per §2.4 above
- §12 "Configuration": swap `warmup_schedule` for `ses_managed_pool_name`
- §13 "Send Rate Limiting & SES Quotas": remove warmup schedule subsection; keep per-second rate limit and daily quota tracking
- §14 "Observability": add `Managed.DedicatedIpCount` and per-ISP reputation to the metrics list