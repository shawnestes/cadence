# Deployment — single EC2, systemd + Caddy

Target: one small EC2 instance (t3.small or similar), ~10 concurrent users, Aurora Postgres reachable over the network. No containers — native install with `uv`, `pnpm`, `systemd`, and `caddy`.

## Prerequisites

- EC2 Amazon Linux 2023 or Ubuntu 22.04+
- Inbound 80/443 open in the security group
- Aurora cluster reachable from the EC2 SG (5432)
- A domain/subdomain with an A record pointing at the EC2 public IP (for automatic TLS)
- IAM instance profile granting SES send + SNS subscribe (preferred over baked-in keys)

## One-time host setup

```bash
# System packages
sudo dnf install -y git caddy  # or: sudo apt install -y git caddy

# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node 20 + pnpm
curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
sudo dnf install -y nodejs
sudo npm install -g pnpm

# Cadence service user
sudo useradd --system --create-home --home /opt/cadence cadence
sudo mkdir -p /var/log/caddy
sudo chown -R caddy:caddy /var/log/caddy
```

## Clone and first deploy

```bash
sudo -iu cadence
git clone <repo-url> /opt/cadence
cd /opt/cadence
cp .env.example .env
# Edit .env — fill in CADENCE_DATABASE_URL, CADENCE_API_KEY,
# CADENCE_UNSUB_SIGNING_KEY, Anthropic + OTTO settings, etc.
chmod 600 .env

uv sync --frozen
cd frontend && pnpm install --frozen-lockfile && pnpm build && cd ..
uv run alembic upgrade head
```

## Install systemd units

```bash
sudo cp /opt/cadence/deploy/cadence-api.service /etc/systemd/system/
sudo cp /opt/cadence/deploy/cadence-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cadence-api.service cadence-worker.service
```

Verify:

```bash
systemctl status cadence-api.service cadence-worker.service
journalctl -u cadence-api.service -n 50
curl -fsS http://127.0.0.1:8000/health
```

## Install Caddy config

```bash
sudo cp /opt/cadence/deploy/Caddyfile /etc/caddy/Caddyfile
# Replace {$CADENCE_SITE_ADDR:localhost} with your domain, or:
echo 'CADENCE_SITE_ADDR=cadence.example.com' | sudo tee -a /etc/default/caddy
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

Caddy provisions TLS automatically via Let's Encrypt on first request. Watch `journalctl -u caddy -f` to confirm.

## Subsequent deploys

As the `cadence` user:

```bash
cd /opt/cadence
./deploy/deploy.sh
```

The script: pulls `main`, `uv sync`, rebuilds the frontend, runs migrations, and restarts both services. `sudo systemctl restart` inside the script requires the `cadence` user to have a narrow sudoers entry:

```
# /etc/sudoers.d/cadence
cadence ALL=(root) NOPASSWD: /bin/systemctl restart cadence-api.service, /bin/systemctl restart cadence-worker.service
```

## Rollback

```bash
cd /opt/cadence
git checkout <good-sha>
./deploy/deploy.sh
```

If a migration needs to be reversed, run `uv run alembic downgrade <rev>` manually before restarting services. Aurora is backed up automatically — a forward-fix is usually safer than a schema downgrade.

## Ad-hoc operations

```bash
# Tail logs
journalctl -u cadence-api.service -f
journalctl -u cadence-worker.service -f

# One-off migration
cd /opt/cadence && uv run alembic upgrade head

# Shell into the app env
cd /opt/cadence && uv run python
```

## What to check if it's not working

- `systemctl status cadence-api.service` — non-zero exit? Check the logged traceback.
- `curl http://127.0.0.1:8000/health` — 200? If not, uvicorn isn't up.
- `psql "$CADENCE_DATABASE_URL" -c "select 1"` — DB reachable from the box?
- `.env` owned by `cadence`, mode `600`, and contains every required `CADENCE_*` var.
- Caddy: `curl -v https://<domain>/health` — TLS working? DNS propagated?
- SES: `aws ses get-send-quota` (with the instance profile) — quota available and sandbox-off?

## Follow-ups that can't be automated here

- DNS A record + domain TLS (Caddy handles ACME but the DNS must resolve to the EC2 first).
- SES production access request (sandbox is the default).
- SNS → `https://<domain>/webhook/ses` HTTP subscription + confirmation.
- DKIM records added to the sending-domain registrar.
- Slack webhook URL wired into `CADENCE_SLACK_WEBHOOK_URL` in `.env` (alerts no-op until it's set).
