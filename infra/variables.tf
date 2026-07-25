variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "location" {
  description = "Azure region for resources"
  type        = string
  default     = "eastus"
}

variable "project_name" {
  description = "Project name prefix for resources"
  type        = string
  default     = "pop"
}

variable "postgres_admin_username" {
  description = "PostgreSQL admin username"
  type        = string
  default     = "popadmin"
}

variable "postgres_admin_password" {
  description = "PostgreSQL admin password"
  type        = string
  sensitive   = true
}

variable "entra_id_tenant_id" {
  description = "Entra ID tenant ID for App Registration"
  type        = string
}

variable "entra_id_client_id" {
  description = "Existing Entra ID App Registration client ID (optional, creates new if empty)"
  type        = string
  default     = ""
}

variable "entra_id_client_secret" {
  description = "Existing Entra ID App Registration client secret (optional)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "session_secret_key" {
  description = "Session secret key for FastAPI (generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\")"
  type        = string
  sensitive   = true
}

variable "openai_deployment_name" {
  description = "Azure OpenAI deployment name for chat completions"
  type        = string
  default     = "gpt-4o-mini"
}

variable "embedding_deployment_name" {
  description = "Azure OpenAI deployment name for embeddings"
  type        = string
  default     = "text-embedding-3-small"
}

variable "container_app_cpu" {
  description = "CPU cores for Container App (consumption)"
  type        = number
  default     = 0.25
}

variable "container_app_memory" {
  description = "Memory in GB for Container App (consumption)"
  type        = number
  default     = 0.5
}

variable "pipeline_job_cpu" {
  description = "CPU cores for Pipeline Job"
  type        = number
  default     = 0.5
}

variable "pipeline_job_memory" {
  description = "Memory in GB for Pipeline Job"
  type        = number
  default     = 1.0
}

variable "acr_sku" {
  description = "ACR SKU (Basic, Standard, Premium)"
  type        = string
  default     = "Basic"
}

variable "postgres_sku" {
  description = "PostgreSQL Flexible Server SKU"
  type        = string
  default     = "Standard_D2s_v3"
}

variable "postgres_storage_gb" {
  description = "PostgreSQL storage in GB"
  type        = number
  default     = 32
}

variable "log_analytics_retention" {
  description = "Log Analytics retention in days"
  type        = number
  default     = 30
}

variable "vnet_address_space" {
  description = "VNet address space CIDR"
  type        = string
  default     = "10.0.0.0/16"
}

variable "container_apps_subnet_cidr" {
  description = "Container Apps subnet CIDR"
  type        = string
  default     = "10.0.1.0/23"
}

variable "data_subnet_cidr" {
  description = "Data subnet CIDR (PostgreSQL, Storage, Key Vault)"
  type        = string
  default     = "10.0.2.0/24"
}

variable "tags" {
  description = "Common tags for all resources"
  type        = map(string)
  default     = {
    project     = "pipeline-of-pipelines"
    managed_by  = "terraform"
    environment = var.environment
  }
}