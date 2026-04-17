# Azure App Registration — Teams Sentinel (022)

## Option A: Azure CLI (fastest)

```bash
# Install if missing: https://docs.microsoft.com/cli/azure/install-azure-cli
az login

# Create the app (personal + work/school accounts)
az ad app create \
  --display-name "Hermes Teams Sentinel" \
  --sign-in-audience "AzureADandPersonalMicrosoftAccount" \
  --public-client-redirect-uris "https://login.microsoftonline.com/common/oauth2/nativeclient"

# Note the appId from the output — that's your MS_GRAPH_CLIENT_ID
APP_ID="<appId from above>"

# Grant delegated permissions (Chat.Read, ChannelMessage.Read.All, User.Read, offline_access)
GRAPH_SP=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv)

# Permission IDs (MS Graph delegated)
# Chat.Read                   = 2eadaff8-0bce-4198-a6b9-2cfc03a2be3d
# ChannelMessage.Read.All     = 767156cb-16ae-4d10-8f8b-41634210b8ec  (or admin-consented)
# User.Read                   = e1fe6dd8-ba31-4d61-89e7-88639da4683d
# offline_access              = 7427e0e9-2fba-42fe-b0c0-848c9e6a8182

az ad app permission add --id $APP_ID \
  --api 00000003-0000-0000-c000-000000000000 \
  --api-permissions \
    2eadaff8-0bce-4198-a6b9-2cfc03a2be3d=Scope \
    e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope \
    7427e0e9-2fba-42fe-b0c0-848c9e6a8182=Scope

# Allow public client flows (required for device code)
az ad app update --id $APP_ID \
  --set publicClient.redirectUris="['https://login.microsoftonline.com/common/oauth2/nativeclient']"

echo "Done. Set in .env:"
echo "MS_GRAPH_CLIENT_ID=$APP_ID"
```

> Note on `ChannelMessage.Read.All`: this permission requires **admin consent** for work/school
> accounts. For personal Microsoft accounts only (consumer), `Chat.Read` is sufficient for
> 1:1 and group chats. Channels (Teams teams) require the permission above or RSC.

---

## Option B: Azure Portal (manual)

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory** → **App registrations** → **New registration**

2. Fill in:
   - **Name**: `Hermes Teams Sentinel`
   - **Supported account types**: _Accounts in any organizational directory (Any Azure AD directory - Multitenant) and personal Microsoft accounts_
   - **Redirect URI**: Platform = `Public client/native (mobile & desktop)`, URI = `https://login.microsoftonline.com/common/oauth2/nativeclient`

3. Click **Register**. Note the **Application (client) ID** — this is `MS_GRAPH_CLIENT_ID`.

4. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**
   - Add: `Chat.Read`, `User.Read`, `offline_access`
   - Add: `ChannelMessage.Read.All` (only if monitoring Teams channels, needs admin consent)

5. Go to **Authentication** → enable **Allow public client flows** → Save.

6. No client secret needed (device-code is a public client).

---

## Set the env var

```bash
# On VPS, add to /opt/hermes/data/.env:
echo "MS_GRAPH_CLIENT_ID=<your-app-id>" >> /opt/hermes/data/.env

# Also set the webhook base URL so Graph subscriptions point to your VPS:
echo "HERMES_WEBHOOK_BASE_URL=https://your-domain.com" >> /opt/hermes/data/.env
```

> The webhook URL must be HTTPS and publicly reachable. If you don't have a domain,
> use ngrok for testing: `ngrok http 8644` and set `HERMES_WEBHOOK_BASE_URL=https://<ngrok-id>.ngrok.io`

---

## Verify

After restarting Hermes:

```
# In Telegram DM with Hermes:
/teams_connect
```

Hermes will reply with a URL and a code like:
```
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code ABCD-EFGH
```

Open the URL on any device, enter the code, sign in with your Microsoft account.
Hermes confirms: "✅ Teams conectado como user@example.com"

Then:
```
/teams_watch 19:<channel-thread-id>@thread.v2 my-channel
/teams_list
```
