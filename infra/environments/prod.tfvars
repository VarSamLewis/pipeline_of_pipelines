# Production deployment configuration. Replace every placeholder before use.
# Secrets belong in the production GitHub Environment, not this file.

project_id                  = "replace-with-prod-gcp-project-id"
region                      = "us-central1"
environment                 = "prod"
project_name                = "pipeline"
github_repository           = "your-github-org/your-repository"
artifact_bucket_name        = "replace-with-prod-gcp-project-id-pipeline-prod-artifacts"
terraform_state_bucket_name = "replace-with-prod-gcp-project-id-pipeline-prod-tf-state"
serverless_vpc_subnet_cidr  = "10.8.0.0/26"

secret_ids = {
  "database-url"        = "pipeline-prod-database-url"
  "session-secret-key"  = "pipeline-prod-session-secret-key"
  "byok-encryption-key" = "pipeline-prod-byok-encryption-key"
  "clerk-secret-key"    = "pipeline-prod-clerk-secret-key"
}

# GitHub Actions replaces this with the immutable image built for each commit.
container_image = "us-central1-docker.pkg.dev/replace-with-prod-gcp-project-id/pipeline-prod/app:placeholder"

clerk_publishable_key  = "pk_live_replace_me"
clerk_issuer           = "https://your-prod-instance.clerk.accounts.com"
clerk_audience         = "your-prod-api-audience"
clerk_jwks_url         = "https://your-prod-instance.clerk.accounts.com/.well-known/jwks.json"
clerk_sign_in_url      = "https://your-prod-instance.clerk.accounts.com/sign-in"
clerk_jwt_template     = "pipeline-beta"
bootstrap_admin_emails = ["admin@example.com"]

min_instances = 1
max_instances = 10
