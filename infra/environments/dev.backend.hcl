# Terraform backend configuration for development. Create this bucket before
# the first Terraform initialization; Terraform cannot create its own backend.
bucket = "pipelinesdev-512107-pipeline-dev-tf-state"
prefix = "pipeline/dev"
