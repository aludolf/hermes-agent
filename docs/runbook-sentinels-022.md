# Runbook — Teams & Email Sentinels (022)

## Overview

Feature 022 adds two background sentinels to Hermes:

| Sentinel | Protocol | Commands |
|---|---|---|
| Teams | MS Graph webhooks + polling fallback | `/teams_connect`, `/teams_watch`, `/teams_unwatch`, `/teams_list`, `/teams_mute`, `/teams_unmute`, `/teams_quiet` |
| Email | IMAP IDLE + async backfill | `/email_connect`, `/email_list`, `/email_disconnect`, `/email_watch`, `/email_unwatch`, `/email_backfill`, `/email_backfill_status` |

---

## Prerequisites

### Environment variables (add to `/opt/hermes/data/.env`)

```bash
# Required for sentinels to start
HERMES_MASTER_KEY=<fernet-key>          # generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
HERMES_OWNER_TELEGRAM_ID=<telegram-id>

# Teams sentinel (optional — only needed for Teams)
MS_GRAPH_CLIENT_ID=<azure-app-client-id>

# Feature flag (set to "0" to disable sentinels without redeploying)
HERMES_SENTINELS_ENABLED=1
```

### Azure App Registration (Teams only)

1. Create an app at https://portal.azure.com → Azure Active Directory → App Registrations
2. Choose "Accounts in any organizational directory and personal Microsoft accounts"
3. Note the **Application (client) ID** → set as `MS_GRAPH_CLIENT_ID`
4. Add delegated permissions: `Chat.Read`, `ChannelMessage.Read.All`, `offline_access`
5. Enable "Allow public client flows" (device code flow)
6. No client secret needed (device code is public client)

### Nginx — MS Graph webhook endpoint

Add to the Hermes nginx config (typically `/etc/nginx/sites-available/hermes`):

```nginx
location /webhooks/ms-graph {
    proxy_pass http://127.0.0.1:8443/webhooks/ms-graph;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 30s;
    client_max_body_size 1m;
}
```

MS Graph requires the webhook URL to be HTTPS and publicly reachable.

---

## First-time Setup

### Teams

```
# In Telegram DM with Hermes:
/teams_connect
# Follow the device code URL + code shown
# Then watch a channel:
/teams_watch 19:<channel-id>@thread.v2 my-alias
# List active watches:
/teams_list
```

### Email (IMAP app-password)

```
/email_connect personal imap.gmail.com user@gmail.com
# Bot prompts for password — reply with app-password
# Then watch inbox:
/email_watch personal INBOX
# List accounts:
/email_list
# Start historical backfill:
/email_backfill personal --since 2024-01-01
# Check progress:
/email_backfill_status
```

---

## Noise controls

```
# Mute a channel indefinitely:
/teams_mute my-alias

# Mute for 4 hours:
/teams_mute my-alias 4

# Unmute:
/teams_unmute my-alias

# Set quiet hours (UTC, spans midnight):
/teams_quiet my-alias 22-08
```

---

## Operational checks

### Verify sentinels started

Check the gateway logs for:
```
EmailSentinel started: N watcher(s) for owner=<id>
TeamsSentinel started
SentinelExtractionQueue consumer started
```

### Check IMAP watcher state

```sql
SELECT watch_id, alias, connection_state, reconnect_count
FROM mail_watches
JOIN mail_accounts USING (account_id)
WHERE owner_id = '<owner_id>';
```

State machine: `connecting → authenticating → idling → reconnecting_backoff → disabled`

### Check Teams watches

```sql
SELECT watch_id, alias, delivery_mode, subscription_expires_at, mute_until
FROM teams_watches
WHERE owner_id = '<owner_id>';
```

### Check backfill jobs

```sql
SELECT job_id, state, processed_count, failed_count, error_message
FROM backfill_jobs
ORDER BY started_at DESC LIMIT 10;
```

---

## Rollback

Set `HERMES_SENTINELS_ENABLED=0` and restart the gateway. No schema changes are needed — all sentinel tables are additive and harmless when empty.

---

## Key constants (code)

| Constant | Value | File |
|---|---|---|
| `_IDLE_TIMEOUT` | 25 min | `email_sentinel.py` |
| `_MAX_RECONNECT_ATTEMPTS` | 10 | `email_sentinel.py` |
| `_BACKOFF_MAX` | 300 s | `email_sentinel.py` |
| `_SUBSCRIPTION_EXPIRY_SECONDS` | 3600 s | `teams_sentinel.py` |
| `_POLLING_INTERVAL` | 60 s | `teams_sentinel.py` |
| `_DEFAULT_RATE` | 60 msgs/min | `email_backfill.py` |
