# Google Drive OAuth setup

This runbook configures the Google Drive and Google Sheets import
integration. Google OAuth is separate from Clerk: Clerk authenticates the app
user, while each user explicitly connects their own Google account.

## Development URLs

The current development Cloud Run origin is:

```text
https://pipelinesdev-dev-1014005337924.europe-west2.run.app
```

Register these exact values on the OAuth client:

```text
Authorized JavaScript origin:
https://pipelinesdev-dev-1014005337924.europe-west2.run.app

Authorized redirect URI:
https://pipelinesdev-dev-1014005337924.europe-west2.run.app/google/callback
```

Google requires exact URL matches. Do not add `/upload` to either value and do
not add a trailing slash to the redirect URI.

## Enable APIs

In the Google Cloud project that hosts the Cloud Run service, enable:

- Google Drive API
- Google Sheets API
- Google Picker API

Terraform also manages the Drive and Sheets APIs in `infra/main.tf`. Enabling
Picker manually is required before it can be selected as an API-key restriction.

## Configure the Google Auth platform

1. Open **Google Auth platform** in Google Cloud Console.
2. Set branding, a user-support email, and developer contact email.
3. Select **External** because end users connect their own Google accounts.
4. Keep the app in **Testing** during development and add all developers and
   testers under **Audience → Test users**.
5. Under **Data Access**, manually add these scopes:

   ```text
   https://www.googleapis.com/auth/drive.file
   https://www.googleapis.com/auth/spreadsheets.readonly
   ```

`openid` and `email` are standard identity scopes requested by the app and do
not need to be added through the Data Access picker. Before broad release,
review Google's OAuth verification requirements for the selected scopes.

## Create the OAuth web client

In **Google Auth platform → Clients**, create a **Web application** client.
Use the development origin and redirect URI above. Save the generated values:

- OAuth client ID
- OAuth client secret

Never commit the client secret or place it in `.env` or a tracked `.tfvars`
file.

## Create the Picker API key

In **APIs & Services → Credentials**, create an API key and restrict it as
follows.

**Application restrictions:** Websites

```text
https://pipelinesdev-dev-1014005337924.europe-west2.run.app/*
https://docs.google.com/*
```

The `docs.google.com` entry is required because Google Picker renders in an
iframe hosted there.

**API restrictions:**

- Google Picker API
- Google Drive API

The Sheets API is not required for this browser key: server-side Sheets reads
use the connected user's OAuth token.

## Configure the GitHub development environment

In **GitHub repository → Settings → Environments → development**, add:

| Type | Name | Value |
| --- | --- | --- |
| Secret | `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth web-client secret |
| Variable | `GOOGLE_OAUTH_CLIENT_ID` | OAuth web-client ID |
| Variable | `GOOGLE_OAUTH_REDIRECT_URI` | Development callback URL above |
| Variable | `GOOGLE_PICKER_API_KEY` | Restricted Picker API key |

`.github/workflows/deploy-dev.yml` maps these values to Terraform inputs.
Terraform writes the OAuth client secret to Google Secret Manager as
`pipeline-dev-google-oauth-client-secret`, grants the Cloud Run service account
access, and injects it into Cloud Run as `GOOGLE_OAUTH_CLIENT_SECRET`.

The other runtime environment variables are:

```text
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_REDIRECT_URI
GOOGLE_PICKER_API_KEY
```

## Test the connection

After a successful development deployment:

1. Open the development app's `/upload` page.
2. Select **Connect Google Drive**.
3. Sign in with a Google account listed as a test user.
4. Approve the requested file-scoped Drive and read-only Sheets permissions.
5. Select a CSV, XLSX, or Google Sheet using Google Picker.
6. Confirm a new immutable imported file appears in the selected client's file
   library.

If the consent screen or Picker is unavailable, first verify the selected
Google Cloud project, enabled APIs, exact registered URLs, GitHub environment
values, and the Cloud Run revision's environment variables.
