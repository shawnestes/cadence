#!/usr/bin/env bash
# Cron wrapper for the daily send.
#
# Exists because `cron` has no timeout: a bare `uv run ... daily_send` that
# hangs holds a DB connection indefinitely and the only symptom is mail that
# never arrives. The wrapper bounds the run and writes a labelled verdict to
# the log so a bad morning is greppable.
#
# Three outcomes are called out explicitly:
#   * TIMEOUT   killed at CADENCE_DAILY_SEND_TIMEOUT_S
#   * FAILURE   non-zero exit, with the tail of its output
#   * WARNING   rc=0 but queued=0. This is the quiet one: once the list is
#               exhausted every run "succeeds" while sending nothing, so rc=0
#               alone is not proof of a healthy morning.
#
# Usage (from crontab):
#   /opt/cadence/deploy/daily_send_cron.sh --campaign OttoDaily --ramp

set -uo pipefail

CADENCE_DIR="${CADENCE_DIR:-/opt/cadence}"
UV="${UV_BIN:-/root/.local/bin/uv}"
TIMEOUT_S="${CADENCE_DAILY_SEND_TIMEOUT_S:-600}"

ts() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }

cd "$CADENCE_DIR" || {
  echo "$(ts) FATAL cannot cd to $CADENCE_DIR"
  exit 1
}

echo "$(ts) daily_send starting: $*"

# --kill-after escalates to SIGKILL if the process ignores SIGTERM, so a wedged
# DB call cannot outlive the window.
out="$(timeout --signal=TERM --kill-after=30s "$TIMEOUT_S" \
       "$UV" run --frozen python -m app.cli.daily_send "$@" 2>&1)"
rc=$?

printf '%s\n' "$out"

if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  echo "$(ts) TIMEOUT after ${TIMEOUT_S}s - no mail queued this morning"
  exit "$rc"
fi

if [ "$rc" -ne 0 ]; then
  echo "$(ts) FAILURE exit=${rc} tail=$(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
  exit "$rc"
fi

# Success path. queued=0 still deserves a flag -- see the header note.
if printf '%s' "$out" | grep -q 'queued=0 '; then
  echo "$(ts) WARNING queued 0 recipients - list may be exhausted or all candidates suppressed/already mailed"
  exit 0
fi

echo "$(ts) OK $(printf '%s' "$out" | grep -oE 'queued=[0-9]+' | head -1)"
exit 0
