# Production Runbook

## Before Deploying

1. Create a dedicated GCP project and use an encrypted, access-restricted
   remote Terraform state backend.
2. Set `project_id`, `region`, `container_image`, Clerk values, and
   `bootstrap_admin_emails` in `infra/terraform.tfvars`.
3. Provide `TF_VAR_postgres_password`, `TF_VAR_session_secret_key`, and any
   platform `TF_VAR_openai_api_key` through the CI secret store. Do not put
   those values in `terraform.tfvars` or source control.
4. Create a Clerk JWT template named by `CLERK_JWT_TEMPLATE`. It must include
   a verified `email` claim and use `CLERK_AUDIENCE`.
5. Configure the Clerk hosted sign-in return URL as
   `https://<service-host>/auth/callback` after the Cloud Run URL is known.

## Deploy

1. Bootstrap Artifact Registry and Cloud Build writer access as described in
   `infra/README.md`.
2. Build and push an immutable image with `cloudbuild.yaml` using a Git SHA
   tag. Set that immutable URI in `container_image`.
3. Run `terraform plan` and review all infrastructure and IAM changes.
4. Run `terraform apply`.
5. Confirm `GET /health` returns `{"status":"ok"}` from Cloud Run.

## Bootstrap Access

1. Sign in to Clerk using an email in `bootstrap_admin_emails`.
2. The verified Clerk identity is provisioned as a platform admin without an
   invitation.
3. Create a client and use `POST /admin/invitations` to invite each operator.
4. Remove bootstrap emails from Terraform after durable administrators have
   been provisioned if the initial bootstrap access is no longer required.

## Required Controls

- Set `APP_ENVIRONMENT=production`; startup rejects missing session secrets,
  insecure cookies, auth bypass, and an empty bootstrap-admin allowlist.
- Keep `AUTH_BYPASS_LOCAL=false` and `SESSION_COOKIE_SECURE=true`.
- Verify the Cloud Run service account has only the Terraform-assigned GCS,
  Cloud SQL, Secret Manager, logging, monitoring, and Vertex permissions.
- Client memberships limit tenant data access; audit-log access is admin-only.
- Use `X-OpenAI-Api-Key` only for request-scoped BYOK. It is not persisted;
  provider URLs remain server configured.

## Verification

1. Confirm Clerk login returns to `/auth/callback` and `/auth/me` returns the
   expected user.
2. Confirm an uninvited, non-bootstrap Clerk identity receives `403`.
3. Confirm an invited operator can access only their assigned client.
4. Upload a small source file, generate a mapping, execute it, and verify the
   generated artifacts appear in the GCS bucket.
5. Confirm Cloud Logging receives application logs and Cloud Monitoring emits
   Cloud Run metrics.

## Rollback

1. Set `container_image` to the preceding immutable Artifact Registry tag.
2. Run `terraform apply` and verify `/health` plus Clerk login.
3. Do not delete the Cloud SQL instance, GCS bucket, or Secret Manager secrets
   as part of an application rollback.

The application creates its schema directly at startup. This deployment model
assumes a fresh database, per the rebuild decision; do not apply it to a
database that must preserve an older schema.
