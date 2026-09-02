variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
}

variable "tenant_id" {
  description = "Azure AD (Entra ID) tenant ID"
  type        = string
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "eastus"
}

variable "owner" {
  description = "Owner tag"
  type        = string
  default     = "pipeline-team"
}

variable "project_name" {
  description = "Project name prefix"
  type        = string
  default     = "pop"
}

variable "container_image_tag" {
  description = "Image tag for the API container in ACR"
  type        = string
  default     = "latest"
}

variable "app_public_url" {
  description = "Public FQDN (no scheme) for the deployed app; used for the Entra ID redirect URI. Leave empty for dev (uses localhost) and set after first apply when the App Service hostname is known."
  type        = string
  default     = ""
}

# --- App Service ---

variable "app_service_sku" {
  description = "App Service Plan SKU (Linux). S1 = 1 vCPU / 3.5 GiB."
  type        = string
  default     = "S1"
}

# --- PostgreSQL Flexible Server ---

variable "database_username" {
  description = "PostgreSQL Flexible Server administrator username"
  type        = string
  default     = "popadmin"
}

variable "database_password" {
  description = "PostgreSQL Flexible Server administrator password. Stored in Key Vault, never in state env."
  type        = string
  sensitive   = true
}

variable "database_sku" {
  description = "PostgreSQL Flexible Server SKU"
  type        = string
  default     = "B_Standard_B1ms"
}

variable "database_version" {
  description = "PostgreSQL Flexible Server major version"
  type        = string
  default     = "16"
}

variable "database_storage_mb" {
  description = "PostgreSQL Flexible Server storage in MB"
  type        = number
  default     = 32768
}

# --- OpenAI ---

variable "azure_openai_api_key" {
  description = "Azure OpenAI API key. Stored in Key Vault, never in state env."
  type        = string
  sensitive   = true
}

variable "openai_chat_model" {
  description = "Deployment/model name for chat completions"
  type        = string
  default     = "gpt-4o-mini"
}

variable "openai_embedding_model" {
  description = "Deployment/model name for embeddings"
  type        = string
  default     = "text-embedding-3-small"
}

variable "openai_api_version" {
  description = "Azure OpenAI API version"
  type        = string
  default     = "2024-10-21"
}

# --- Session ---

variable "session_secret_key" {
  description = "FastAPI session secret. Stored in Key Vault, never in state env."
  type        = string
  sensitive   = true
}

# --- Entra ID ---

variable "entra_client_secret" {
  description = "Client secret of the Entra ID app registration. Stored in Key Vault, never in state env."
  type        = string
  sensitive   = true
}

variable "entra_tenant_id" {
  description = "Entra ID tenant ID for the app (used for OIDC authority)"
  type        = string
}

# --- Tags ---

variable "tags" {
  description = "Additional tags for all resources"
  type        = map(string)
  default     = {}
}
