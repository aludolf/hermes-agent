#!/usr/bin/env bash
# deploy-022.sh — Deploy 022 Teams+Email sentinel changes to VPS
#
# Usage:
#   bash scripts/deploy-022.sh [user@host] [remote-root]
#
# Defaults:
#   user@host    = root@129.121.51.4 (SSH config alias: personalos-vps)
#   remote-root  = /opt/hermes
#   ssh port     = 22022
#
# What this script does:
#   1. Generates HERMES_MASTER_KEY on remote if not set
#   2. Syncs changed source files via tar pipe
#   3. Restarts the hermes gateway service

set -euo pipefail

REMOTE="${1:-root@129.121.51.4}"
REMOTE_ROOT="${2:-/opt/hermes}"
SSH_PORT="${SSH_PORT:-22022}"
SSH_CMD="ssh -p $SSH_PORT"
SCP_CMD="scp -P $SSH_PORT"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Deploying to $REMOTE:$REMOTE_ROOT (port $SSH_PORT)"

# ---------------------------------------------------------------------------
# Step 1: Ensure HERMES_MASTER_KEY is set on remote
# ---------------------------------------------------------------------------
echo ""
echo "==> [1/4] Checking HERMES_MASTER_KEY on remote..."

$SSH_CMD "$REMOTE" bash -s <<'REMOTE_SCRIPT'
ENV_FILE="/opt/hermes/data/.env"
if ! grep -q "^HERMES_MASTER_KEY=" "$ENV_FILE" 2>/dev/null || \
   grep -q "^HERMES_MASTER_KEY=$" "$ENV_FILE" 2>/dev/null; then
  echo "  Generating new HERMES_MASTER_KEY..."
  KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  if grep -q "^HERMES_MASTER_KEY=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^HERMES_MASTER_KEY=.*|HERMES_MASTER_KEY=$KEY|" "$ENV_FILE"
  else
    echo "HERMES_MASTER_KEY=$KEY" >> "$ENV_FILE"
  fi
  echo "  HERMES_MASTER_KEY generated and saved to $ENV_FILE"
else
  echo "  HERMES_MASTER_KEY already set — skipping"
fi
REMOTE_SCRIPT

# ---------------------------------------------------------------------------
# Step 2: Ensure sentinel env vars are present (non-overwriting)
# ---------------------------------------------------------------------------
echo ""
echo "==> [2/4] Ensuring sentinel env defaults in remote .env..."

$SSH_CMD "$REMOTE" bash -s <<'REMOTE_SCRIPT'
ENV_FILE="/opt/hermes/data/.env"
add_if_missing() {
  local key="$1" default="$2"
  if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    echo "${key}=${default}" >> "$ENV_FILE"
    echo "  Added: ${key}=${default}"
  fi
}
add_if_missing "HERMES_SENTINELS_ENABLED" "1"
add_if_missing "MS_GRAPH_REDIRECT_URI" "https://login.microsoftonline.com/common/oauth2/nativeclient"
# MS_GRAPH_CLIENT_ID and HERMES_WEBHOOK_BASE_URL must be set manually
for required in MS_GRAPH_CLIENT_ID HERMES_WEBHOOK_BASE_URL; do
  if ! grep -q "^${required}=" "$ENV_FILE" 2>/dev/null || \
     grep -q "^${required}=$" "$ENV_FILE" 2>/dev/null; then
    echo "  WARNING: $required is not set in $ENV_FILE — Teams sentinel will not connect"
  fi
done
REMOTE_SCRIPT

# ---------------------------------------------------------------------------
# Step 3: Sync source files
# ---------------------------------------------------------------------------
echo ""
echo "==> [3/4] Syncing 022 source files..."

cd "$REPO_ROOT"

# Files changed in this feature (phases 3-7)
FILES=(
  agent/orchestrator/teams_auth.py
  agent/orchestrator/sentinel_queue.py
  agent/orchestrator/teams_sentinel.py
  agent/orchestrator/teams_webhook.py
  agent/orchestrator/email_auth.py
  agent/orchestrator/email_sentinel.py
  agent/orchestrator/email_backfill.py
  agent/orchestrator/__init__.py
  gateway/run.py
  gateway/platforms/webhook.py
  hermes_cli/commands.py
  hermes_state.py
)

tar czf - "${FILES[@]}" | $SSH_CMD "$REMOTE" \
  "cd ${REMOTE_ROOT}/repo && tar xzf - && echo '  Files extracted OK'"

# ---------------------------------------------------------------------------
# Step 4: Restart hermes service
# ---------------------------------------------------------------------------
echo ""
echo "==> [4/4] Restarting hermes gateway..."

$SSH_CMD "$REMOTE" bash -s <<REMOTE_SCRIPT
if systemctl is-active --quiet hermes-gateway 2>/dev/null; then
  systemctl restart hermes-gateway
  sleep 3
  if systemctl is-active --quiet hermes-gateway; then
    echo "  hermes-gateway restarted OK"
    systemctl status hermes-gateway --no-pager -l | tail -5
  else
    echo "  ERROR: hermes-gateway failed to start"
    journalctl -u hermes-gateway --no-pager -n 30
    exit 1
  fi
elif [ -f "${REMOTE_ROOT}/repo/scripts/hermes-gateway" ]; then
  # Fallback: restart via script
  "${REMOTE_ROOT}/repo/scripts/hermes-gateway" restart
  echo "  Service restarted via script"
else
  echo "  WARNING: Could not detect service manager — restart manually"
fi
REMOTE_SCRIPT

echo ""
echo "==> Deploy complete."
echo ""
echo "Next steps:"
echo "  1. In Telegram DM with Hermes, run /teams_connect"
echo "     Follow the device code URL printed in the reply."
echo "  2. Then /teams_watch <channel-id> <alias> to start monitoring."
echo "  3. Check sentinel startup in logs:"
echo "     journalctl -u hermes-gateway -f"
