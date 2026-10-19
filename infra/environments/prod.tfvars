# Production deployment configuration. Replace every placeholder before use.
# Secrets belong in the production GitHub Environment, not this file.

project_id        = "replace-with-prod-gcp-project-id"
region            = "us-central1"
environment       = "prod"
project_name      = "pipeline"
github_repository = "your-github-org/your-repository"

# GitHub Actions replaces this with the immutable image built for each commit.
container_image = "us-central1-docker.pkg.dev/replace-with-prod-gcp-project-id/pipeline-prod/app:placeholder"

clerk_publishable_key    = "pk_live_replace_me"
clerk_issuer             = "https://your-prod-instance.clerk.accounts.com"
clerk_audience           = "your-prod-api-audience"
clerk_jwks_url           = "https://your-prod-instance.clerk.accounts.com/.well-known/jwks.json"
clerk_sign_in_url        = "https://your-prod-instance.clerk.accounts.com/sign-in"
clerk_jwt_template       = "pipeline-beta"
bootstrap_admin_emails   = ["admin@example.com"]

min_instances = 1
max_instances = 10
