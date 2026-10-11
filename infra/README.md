# GCP Deployment

This Terraform stack deploys the API to Cloud Run with a private-IP Cloud SQL
PostgreSQL database, Cloud Storage for pipeline artifacts, Artifact Registry,
Secret Manager, and a least-privilege Cloud Run service account.

## Deploy

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
   `TF_VAR_session_secret_key`, and `TF_VAR_openai_api_key` through a secure
   CI secret store.
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
