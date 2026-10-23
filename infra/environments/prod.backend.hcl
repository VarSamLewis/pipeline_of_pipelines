# Terraform backend configuration for production. Create this bucket before
# the first Terraform initialization; Terraform cannot create its own backend.
bucket = "replace-with-prod-terraform-state-bucket"
prefix = "pipeline/prod"
