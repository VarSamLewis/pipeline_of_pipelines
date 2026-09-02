locals {
  project_name    = var.project_name
  environment     = var.environment
  resource_prefix = "${local.project_name}-${local.environment}"

  tags = merge(
    {
      Project     = "Pipeline of Pipelines"
      Environment = local.environment
      ManagedBy   = "Terraform"
      Owner       = var.owner
    },
    var.tags,
  )
}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
  numeric = true
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.resource_prefix}-${random_string.suffix.result}"
  location = var.location
  tags     = local.tags
}
