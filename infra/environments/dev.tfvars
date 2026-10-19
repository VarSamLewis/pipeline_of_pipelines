# Development deployment configuration. Replace every placeholder before use.
# Secrets belong in the development GitHub Environment, not this file.

project_id        = "replace-with-dev-gcp-project-id"
region            = "us-central1"
environment       = "dev"
project_name      = "pipeline"
github_repository = "your-github-org/your-repository"

# GitHub Actions replaces this with the immutable image built for each commit.
container_image = "us-central1-docker.pkg.dev/replace-with-dev-gcp-project-id/pipeline-dev/app:placeholder"

clerk_publishable_key    = "pk_test_replace_me"
clerk_issuer             = "https://your-dev-instance.clerk.accounts.dev"
clerk_audience           = "your-dev-api-audience"
clerk_jwks_url           = "https://your-dev-instance.clerk.accounts.dev/.well-known/jwks.json"
clerk_sign_in_url        = "https://your-dev-instance.clerk.accounts.dev/sign-in"
clerk_jwt_template       = "pipeline-beta"
bootstrap_admin_emails   = ["admin@example.com"]

min_instances = 0
max_instances = 3
