# GCP Deployment

This Terraform stack deploys the API to Cloud Run with a private-IP Cloud SQL
PostgreSQL database, Cloud Storage for pipeline artifacts, Artifact Registry,
Secret Manager, and a least-privilege Cloud Run service account.

## GitHub Actions OIDC Bootstrap

The deployment workflows authenticate with GitHub OIDC; they do not store a GCP
service-account key. This requires one administrator-performed bootstrap per
environment before the workflows can deploy.

1. Set `terraform_state_bucket_name` in the environment tfvars. Terraform
   creates this versioned bucket in the same GCP project as the application.
2. Edit `environments/dev.tfvars` and `environments/prod.tfvars` with each
   environment's GCP project, artifact bucket name, Secret Manager secret IDs,
   GitHub repository, Clerk configuration, and admin email. These tracked files
   are deliberately non-secret.
3. Edit `environments/dev.backend.hcl` and
   `environments/prod.backend.hcl` with their corresponding Terraform state
   bucket names. Terraform backends must use separate files because Terraform
   initializes the backend before it reads tfvars.
4. Bootstrap each state bucket once with local state, then migrate state:

   Export the required `TF_VAR_postgres_password`, `TF_VAR_session_secret_key`,
   `TF_VAR_byok_encryption_key`, and `TF_VAR_clerk_secret_key` values first.

   ```bash
   terraform -chdir=infra init -backend=false
   terraform -chdir=infra apply -var-file=environments/dev.tfvars \
     -target=google_storage_bucket.terraform_state
   terraform -chdir=infra init -migrate-state \
     -backend-config=environments/dev.backend.hcl
   ```

   Repeat with the production configuration. After migration, normal Terraform
   commands and GitHub Actions use the managed GCS backend.
5. Run the initial full `terraform plan` and `terraform apply` with a
   privileged administrator identity. This creates the Workload Identity Pool,
   OIDC provider, and GitHub deployer service account.
6. Record the `github_workload_identity_provider` and
   `github_deployer_service_account` Terraform outputs.
7. Create GitHub Environments named `development` and `production`. Require
   reviewers for `production` in GitHub Environment protection rules.
8. Configure each environment with these GitHub Variables:

   ```text
   ARTIFACT_REPOSITORY
   BOOTSTRAP_ADMIN_EMAILS       # Terraform list syntax, e.g. ["admin@example.com"]
   GCP_DEPLOYER_SERVICE_ACCOUNT
   GCP_PROJECT_ID
   GCP_REGION
   GCP_WORKLOAD_IDENTITY_PROVIDER
   ```

9. Configure each environment with these GitHub Secrets:

   ```text
   BYOK_ENCRYPTION_KEY
   CLERK_SECRET_KEY
   POSTGRES_PASSWORD
   SESSION_SECRET_KEY
   ```

`develop` triggers `deploy-dev.yml` and deploys the `dev` stack. `main`
triggers `deploy-prod.yml`; GitHub Environment approval gates production before
any cloud command runs.

The workflows use `environments/dev.tfvars` and `environments/prod.tfvars`,
with their matching backend files. The immutable container image is the only
value overridden by the workflow.

## Candidate worker network permissions

The optional candidate worker runs model-authored transformation code in a
separate Cloud Run service. It uses Direct VPC egress and a targeted firewall
rule which denies all outbound traffic. The denial prevents a candidate script
from sending source data to arbitrary network endpoints; the worker has no app
secrets, Cloud SQL mount, or project IAM roles.

Terraform creates these resources even while `candidate_worker_enabled =
false`; that flag only controls whether the API sends execution requests to the
worker. The worker has `min_instance_count = 0`, so it has no idle Cloud Run
instance cost.

Creating and maintaining the targeted denial rule requires these Compute API
permissions for the identity that runs Terraform:

```text
compute.firewalls.create
compute.firewalls.delete
compute.firewalls.get
compute.firewalls.list
compute.firewalls.update
compute.networks.get
```

The Terraform-managed GitHub deployer is currently assigned the broader
`roles/compute.networkAdmin` role, which includes the required firewall
permissions. A `403 Required 'compute.firewalls.create' permission` therefore
usually means that the workflow is authenticating as a *different* service
account than the one configured by Terraform, or that its IAM change has not
yet propagated. Check that the GitHub Environment variable
`GCP_DEPLOYER_SERVICE_ACCOUNT` exactly equals the
`github_deployer_service_account` Terraform output for that environment.

For an externally managed least-privilege role, an administrator can create a
custom role and bind it to the actual deployment service account:

```bash
gcloud iam roles create candidateFirewallDeployer \
  --project=<project-id> \
  --title="Candidate firewall deployer" \
  --description="Manages the isolated candidate worker firewall rule" \
  --permissions=compute.firewalls.create,compute.firewalls.delete,compute.firewalls.get,compute.firewalls.list,compute.firewalls.update,compute.networks.get \
  --stage=GA

gcloud projects add-iam-policy-binding <project-id> \
  --member="serviceAccount:<actual-deployer-service-account>" \
  --role="projects/<project-id>/roles/candidateFirewallDeployer"
```

Do not grant these permissions to the candidate worker service account. It is
intentionally unprivileged. Also, because this Terraform stack currently grants
`roles/compute.networkAdmin` to its GitHub deployer, replacing that broad grant
with the custom role requires a corresponding Terraform change; otherwise a
later apply can restore the broad role.

## Cloud SQL Stop/Start Schedule

`db_stop_schedule` and `db_start_schedule` accept Cloud Scheduler cron
expressions in UTC. The dev environment stops its database at 00:00 and starts
it at 07:00, pausing compute billing for the off window. Stopping preserves all
data, backups, and settings; storage and backup charges continue. Keep the
instance's backup window (`start_time` on the backup configuration) inside the
on-hours because automated backups only run while the instance is up. Omit both
variables to leave the instance always on. `db_tier` selects the machine tier;
the default `db-custom-1-3840` is a dedicated-core machine, while shared-core
tiers such as `db-g1-small` cost roughly half and suit development workloads
(their 50-connection cap bounds Cloud Run instance count and pool overflow).

## Manual Deploy

1. Authenticate Terraform to the target GCP project and ensure the deploying
   principal can enable APIs, administer Cloud SQL and Secret Manager, create
   IAM bindings, and deploy Cloud Run.
2. Bootstrap the Artifact Registry repository with:
   `terraform apply -target=google_artifact_registry_repository.app -target=google_artifact_registry_repository_iam_member.cloud_build`.
3. Build and publish an immutable image with Cloud Build:
   `gcloud builds submit --config cloudbuild.yaml --substitutions=_REGION=<region>,_REPOSITORY=<repository>,_IMAGE_TAG=<git-sha>`.
   Set `container_image` to the resulting Artifact Registry URI.
4. Copy `terraform.tfvars.example` to `terraform.tfvars` and set non-secret
   values. Supply secret inputs as `TF_VAR_postgres_password`,
   `TF_VAR_session_secret_key`, `TF_VAR_byok_encryption_key`,
   `TF_VAR_clerk_secret_key` through a secure CI secret store.
5. Run `terraform init`, `terraform plan`, then `terraform apply` from this
   directory.
6. Configure Clerk to return from hosted sign-in to
   `<cloud_run_url>/auth/callback`. Create the configured JWT template with a
   verified `email` claim and the configured audience before authenticating
   users.

Terraform necessarily tracks the secret versions it creates. Use a protected,
encrypted remote Terraform state backend with restricted access. For production,
prefer creating secret versions through a separate approved secret-delivery
process and remove the version resources from Terraform.

See `../docs/production_runbook.md` for the Clerk bootstrap and production
verification procedure.
