#!/bin/bash
# Setup productivity cron jobs for Hermes (003 — Phase 5)
#
# Run ONCE after deploying Phase 5. Uses the built-in /cron command
# via the Hermes CLI inside the container.
#
# Usage (from VPS):
#   docker exec hermes-gateway bash /opt/hermes/scripts/setup_productivity_crons.sh
#
# Or via SSH:
#   ssh personalos-vps "docker exec hermes-gateway bash /opt/hermes/scripts/setup_productivity_crons.sh"

set -e

HERMES="/opt/hermes/.venv/bin/python -m hermes_cli.cron"
OWNER_ID="${HERMES_OWNER_TELEGRAM_ID:-289322060}"

echo "Setting up productivity cron jobs..."
echo "Owner Telegram ID: $OWNER_ID"

# Morning briefing: 08:00 BRT daily (11:00 UTC)
# Uses the BriefingBuilder via /briefing command
echo ""
echo "=== Morning Briefing (08:00 BRT daily) ==="
$HERMES add "0 11 * * *" \
  "Gere o briefing matinal completo usando o comando /briefing e envie o resultado." \
  --deliver "telegram:$OWNER_ID" \
  --name "morning_briefing" \
  2>&1 || echo "  (may already exist)"

# Meeting reminder: every 15 minutes
# Checks upcoming calendar events and sends reminders
echo ""
echo "=== Meeting Reminder (every 15 min) ==="
$HERMES add "*/15 * * * *" \
  "Verifique se há reuniões nos próximos 30 minutos no Google Calendar. Para cada reunião que ainda não foi lembrada, envie um lembrete ao usuário com o nome da reunião, horário e local." \
  --deliver "telegram:$OWNER_ID" \
  --name "meeting_reminder" \
  2>&1 || echo "  (may already exist)"

echo ""
echo "Done. Use 'hermes cron list' to verify."
