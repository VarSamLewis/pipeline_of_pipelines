# Environment parameters for the dev environment (non-secret).
# Secrets live in secrets.tfvars (gitignored).
#
# Usage:
#   terraform plan  -var-file=params.tfvars -var-file=secrets.tfvars

subscription_id = "00000000-0000-0000-0000-000000000000"
tenant_id       = "00000000-0000-0000-0000-000000000000"
entra_tenant_id = "00000000-0000-0000-0000-000000000000"

environment = "dev"
location    = "eastus"

owner        = "pipeline-team"
project_name = "pop"
tags = {
  Env = "dev"
}

app_service_sku = "S1"
app_public_url  = ""

container_image_tag = "latest"

database_username   = "popadmin"
database_sku        = "B_Standard_B1ms"
database_version    = "16"
database_storage_mb = 32768

openai_chat_model      = "gpt-4o-mini"
openai_embedding_model = "text-embedding-3-small"
openai_api_version     = "2024-10-21"