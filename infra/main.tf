locals {
  # Naming convention: <project>-<env>-<resource>-<region>
  project_name   = "pop"
  environment    = var.environment
  location       = var.location
  resource_prefix = "${local.project_name}-${local.environment}"
  tags = {
    Project     = "Pipeline of Pipelines"
    Environment = local.environment
    ManagedBy   = "Terraform"
    Owner       = var.owner
  }

  # Common name suffix for resources requiring globally unique names
  unique_suffix = var.unique_suffix

  # VNet address space
  vnet_address_space = "10.0.0.0/16"

  # Subnet CIDRs
  subnet_cidrs = {
    container_apps = "10.0.0.0/20"
    postgres       = "10.0.16.0/24"
    storage        = "10.0.32.0/24"
    acr            = "10.0.48.0/24"
    ai_services    = "10.0.64.0/24"
  }

  # Container Apps environment name
  aca_env_name = "${local.resource_prefix}-aca"

  # Log Analytics workspace
  log_analytics_name = "${local.resource_prefix}-logs"
}

resource "azurerm_resource_group" "main" {
  name     = "${local.resource_prefix}-rg"
  location = local.location
  tags     = local.tags
}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
  number  = true
}