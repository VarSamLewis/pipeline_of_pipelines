# Development Test Checklist

Run these checks after a successful development deployment.

## Prerequisites

- The GitHub `development` Environment contains `BOOTSTRAP_ADMIN_EMAILS` with
  a verified Clerk email address.
- The Cloud Run deployment workflow completed successfully.
- Cloud SQL is running.

## Database Schema

Application tables are created in the PostgreSQL `pipeline` schema. For a
development reset, drop only that schema rather than `public`:

```sql
DROP SCHEMA pipeline CASCADE;
```

The next application startup recreates the schema and tables.

Set the service URL once for command-line checks:

```bash
export SERVICE_URL="https://pipelinesdev-dev-1014005337924.europe-west2.run.app"
```

## Service Health

```bash
curl --fail --show-error "$SERVICE_URL/health"
```

Expected response:

```json
{"status":"ok"}
```

## Clerk Redirect

```bash
curl --include --silent "$SERVICE_URL/auth/login"
```

Confirm the `location` header points to the Clerk Account Portal and includes
an HTTPS callback URL ending in `/auth/callback`.

## Browser Sign-In

1. Open a private/incognito browser window.
2. Visit `$SERVICE_URL/login`.
3. Confirm Clerk displays its hosted sign-in page.
4. Sign in with the verified bootstrap-admin email.
5. Confirm Clerk returns to the application rather than showing an error page.
6. Visit `$SERVICE_URL/auth/me` in the authenticated browser session.
7. Confirm the response identifies the expected email and admin role.

## Authorization Checks

1. Sign out and confirm protected pages redirect to `/login`.
2. Sign in with a non-bootstrap user that has no invitation and confirm access
   is denied.
3. Invite a reviewer from the application, sign in as that reviewer, and
   confirm the reviewer can access only assigned client data.

## Pipeline Smoke Test

1. Create an organization/client.
2. Upload a small CSV file and a target-schema JSON file.
3. Generate a mapping proposal.
4. Review and approve the mapping.
5. Generate and execute the pipeline.
6. Confirm results appear in the application.
7. Confirm generated artifacts are present in the GCS artifact bucket under
   the relevant output folder.

## Post-Test Checks

1. Review Cloud Run logs for application errors.
2. Review Cloud SQL metrics and Cloud Run request/error metrics.
3. If testing is complete, stop the development Cloud SQL instance to reduce
   idle compute cost.
