terraform {
  required_version = ">= 1.7"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.53"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Backend is configured via partial config in backend.hcl, passed with:
  #   terraform init -backend-config=backend.hcl
  # Swapping to Azure remote state later = replacing the contents of backend.hcl.
  backend "local" {}
}

# Variables are supplied via -var-file:
#   terraform plan  -var-file=params.tfvars -var-file=secrets.tfvars
#   terraform apply -var-file=params.tfvars -var-file=secrets.tfvars
# and forwarded verbatim to the shared `pop` module.

variable "subscription_id" {
  type = string
}

variable "tenant_id" {
  type = string
}

variable "environment" {
  type = string
}

variable "location" {
  type = string
}

variable "owner" {
  type = string
}

variable "project_name" {
  type = string
}

variable "tags" {
  type = map(string)
}

variable "database_username" {
  type = string
}

variable "database_password" {
  type      = string
  sensitive = true
}

variable "database_sku" {
  type = string
}

variable "database_version" {
  type = string
}

variable "database_storage_mb" {
  type = number
}

variable "azure_openai_api_key" {
  type      = string
  sensitive = true
}

variable "openai_chat_model" {
  type = string
}

variable "openai_embedding_model" {
  type = string
}

variable "openai_api_version" {
  type = string
}

variable "session_secret_key" {
  type      = string
  sensitive = true
}

variable "entra_tenant_id" {
  type = string
}

variable "role_group_object_ids" {
  type = map(list(string))
}

variable "container_image_tag" {
  type = string
}

variable "app_public_url" {
  type = string
}

variable "app_service_sku" {
  type = string
}

module "pop" {
  source = "../../modules/root"

  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
  environment     = var.environment
  location        = var.location
  owner           = var.owner
  project_name    = var.project_name
  tags            = var.tags

  database_username   = var.database_username
  database_password   = var.database_password
  database_sku        = var.database_sku
  database_version    = var.database_version
  database_storage_mb = var.database_storage_mb

  azure_openai_api_key   = var.azure_openai_api_key
  openai_chat_model      = var.openai_chat_model
  openai_embedding_model = var.openai_embedding_model
  openai_api_version     = var.openai_api_version

  session_secret_key    = var.session_secret_key
  entra_tenant_id       = var.entra_tenant_id
  role_group_object_ids = var.role_group_object_ids
  container_image_tag   = var.container_image_tag
  app_public_url        = var.app_public_url
  app_service_sku       = var.app_service_sku
}