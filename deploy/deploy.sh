#!/usr/bin/env bash
# Cadence deploy script — run on the EC2 host as the `cadence` user.
# Pulls latest main, rebuilds Python env + frontend, runs migrations, restarts services.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cadence}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"

echo "[1/5] git pull origin $BRANCH"
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "[2/5] uv sync (Python deps)"
uv sync --frozen

echo "[3/5] frontend build"
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..

echo "[4/5] alembic migrate"
uv run alembic upgrade head

echo "[5/5] restart services"
sudo systemctl restart cadence-api.service
sudo systemctl restart cadence-worker.service

echo "Done. Check logs:"
echo "  journalctl -u cadence-api.service -f"
echo "  journalctl -u cadence-worker.service -f"
