# Clerk setup

This runbook configures Clerk as the application's login provider. It is
separate from Google Drive OAuth: Clerk identifies and authorizes the person
using Pipeline of Pipelines; Google OAuth authorizes that person to import from
their own Drive.

## Values used by the application

The Cloud Run service receives these Clerk environment variables:

| Variable | Purpose | Secret? |
| --- | --- | --- |
| `CLERK_PUBLISHABLE_KEY` | Browser-side Clerk application key | No |
| `CLERK_SECRET_KEY` | Clerk backend API key, used to send invitations | Yes |
| `CLERK_ISSUER` | JWT issuer URL used to verify tokens | No |
| `CLERK_AUDIENCE` | Required JWT audience for browser/API tokens | No |
| `CLERK_CLI_OAUTH_CLIENT_ID` | Public Clerk OAuth client ID accepted for CLI tokens | No |
| `CLERK_JWKS_URL` | JWKS endpoint used to verify JWT signatures | No |
| `CLERK_SIGN_IN_URL` | Clerk-hosted sign-in URL | No |
| `CLERK_JWT_TEMPLATE` | JWT template name used by the app and CLI | No |

`CLERK_SECRET_KEY` is the only Clerk value that must be treated as a secret.
Terraform stores it in Google Secret Manager and Cloud Run reads the latest
version at runtime.

## Create or locate the Clerk application

1. Open the [Clerk Dashboard](https://dashboard.clerk.com/).
2. Select the development Clerk application for this environment, or create a
   development instance.
3. Configure the desired sign-in methods and require verified email addresses.
   The backend rejects identities with an unverified email address.
4. In the Clerk Dashboard's API keys area, copy the publishable key and secret
   key. Do not commit the secret key or share it in chat.

## Configure the application URLs

For development, add the Cloud Run application origin to Clerk's allowed
origins / redirect URLs, using the current development deployment URL:

```text
https://pipelinesdev-dev-1014005337924.europe-west2.run.app
```

The app begins browser sign-in at `/login` and asks Clerk to return to its
server-side callback. Use Clerk's hosted sign-in page as the `CLERK_SIGN_IN_URL`
value. The callback behaviour must preserve the app-provided `redirect_url`
query parameter.

When changing environments, register the new Cloud Run origin with Clerk before
deploying. Do not reuse a development Clerk instance or keys in production.

## Configure the JWT template

The application expects a Clerk JWT template named:

```text
pipeline-beta
```

Create or update that template in Clerk's JWT-template settings. The token must
be signed by the Clerk instance and include the normal JWT timing claims plus a
subject:

```text
sub
exp
iat
nbf
```

For first-time provisioning, the backend also needs a verified email address.
Ensure the token contains either `email` or `email_address`; when it does not,
the backend retrieves the profile from Clerk's OAuth userinfo endpoint. The
template's configured audience must equal the value deployed as
`CLERK_AUDIENCE`.

Keep the template name in `CLERK_JWT_TEMPLATE` synchronized with the template
name used by browser and CLI clients.

## Configure the CLI OAuth client

The `pop` CLI uses Clerk's OAuth device-authorization flow. In Clerk, create
or identify the public OAuth client used for this flow and set its client ID as:

```text
CLERK_CLI_OAUTH_CLIENT_ID
```

The backend accepts a token without the normal audience only when its
`client_id` matches this value. Do not use the OAuth client secret in the CLI or
commit it to this repository.

## Configure GitHub and Terraform

In **GitHub repository → Settings → Environments → development**, add the
following environment secret:

```text
CLERK_SECRET_KEY
```

The development deployment workflow passes it as
`TF_VAR_clerk_secret_key`. Terraform writes it to the environment-specific
Secret Manager secret named by `infra/environments/dev.tfvars`, grants the
Cloud Run runtime service account access, and injects it as
`CLERK_SECRET_KEY`.

Set the non-secret Clerk configuration in the appropriate environment tfvars
file, for example `infra/environments/dev.tfvars`:

```hcl
clerk_publishable_key     = "pk_test_..."
clerk_issuer              = "https://<instance>.clerk.accounts.dev"
clerk_audience            = "pipeline-dev-api"
clerk_cli_oauth_client_id = "<public-oauth-client-id>"
clerk_jwks_url            = "https://<instance>.clerk.accounts.dev/.well-known/jwks.json"
clerk_sign_in_url         = "https://<instance>.accounts.dev/sign-in"
clerk_jwt_template        = "pipeline-beta"
```

`bootstrap_admin_emails` is also set in the environment tfvars. These emails
receive the platform-admin role when they authenticate. All other users must
have a pending in-app invitation before Clerk login provisions them.

## Verify after deployment

1. Open the deployed application while signed out.
2. Visit `/login` and complete Clerk's hosted sign-in flow.
3. Confirm you return to the app and can access `/upload`.
4. Test an invited non-admin user: they should only see their organization.
5. Test `pop login` and `pop whoami` with the CLI OAuth configuration.
6. Review Cloud Run logs for safe authentication errors only; never log JWTs,
   Clerk secret keys, or authorization headers.

## Troubleshooting

- **Redirect loop or rejected callback:** verify the deployed origin is allowed
  in Clerk and that `CLERK_SIGN_IN_URL` targets the correct Clerk instance.
- **Invalid Clerk session token:** verify issuer, audience, JWKS URL, template
  name, and signing instance all belong to the same environment.
- **No pending beta invitation:** create an in-app invitation before a
  non-bootstrap user signs in.
- **Invitation email cannot be sent:** verify the GitHub environment secret,
  Secret Manager version, and Cloud Run `CLERK_SECRET_KEY` configuration.
