# Secrets for the dev environment. NOT COMMITTED (gitignored).
# Copy from the old infra/terraform.tfvars or generate fresh values.
#
# Usage: terraform apply -var-file=params.tfvars -var-file=secrets.tfvars

# Min 8 chars, must include uppercase, lowercase, and number.
database_password = "replace-me"

azure_openai_api_key = "replace-me"

# Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
session_secret_key = "replace-me"