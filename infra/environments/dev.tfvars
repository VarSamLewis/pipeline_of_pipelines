# Development deployment configuration. Replace every placeholder before use.
# Secrets belong in the development GitHub Environment, not this file.

project_id                  = "pipelinesdev-512107"
region                      = "europe-west2"
environment                 = "dev"
project_name                = "pipelinesdev"
github_repository           = "VarSamLewis/pipeline_of_pipelines"
artifact_bucket_name        = "pipelinesdev-512107-pipeline-dev-artifacts"
terraform_state_bucket_name = "pipelinesdev-512107-pipeline-dev-tf-state"
serverless_vpc_subnet_cidr  = "10.8.0.0/26"

secret_ids = {
  "database-url"        = "pipeline-dev-database-url"
  "session-secret-key"  = "pipeline-dev-session-secret-key"
  "byok-encryption-key" = "pipeline-dev-byok-encryption-key"
  "clerk-secret-key"    = "pipeline-dev-clerk-secret-key"
}

# GitHub Actions replaces this with the immutable image built for each commit.
container_image = "europe-west2-docker.pkg.dev/pipelinesdev-512107/pipelinesdev-dev/app:placeholder"

clerk_publishable_key  = "pk_test_cG9zc2libGUtdGFwaXItMjU4Ni5jbGVyay5hY2NvdW50cy5kZXYk"
clerk_issuer           = "https://possible-tapir-2586.clerk.accounts.dev"
clerk_audience         = "pipeline-dev-api"
clerk_jwks_url         = "https://possible-tapir-2586.clerk.accounts.dev/.well-known/jwks.json"
clerk_sign_in_url      = "https://possible-tapir-2586.clerk.accounts.dev/sign-in"
clerk_jwt_template     = "pipeline-beta"
bootstrap_admin_emails = ["admin@example.com"]

min_instances = 0
max_instances = 3
