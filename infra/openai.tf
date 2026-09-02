resource "azurerm_cognitive_account" "openai" {
  name                  = "cog-${replace(local.resource_prefix, "-", "")}"
  resource_group_name   = azurerm_resource_group.main.name
  location              = azurerm_resource_group.main.location
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "cog-${replace(local.resource_prefix, "-", "")}"

  tags = local.tags
}

# Chat deployment. AzureOpenAI SDK uses the deployment name as the model name,
# so MAPPING_MODEL / CODEGEN_MODEL both point at this deployment.
resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.openai_chat_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.openai_chat_model
    version = "latest"
  }

  sku {
    name     = "Standard"
    capacity = 10
  }
}

# Embedding deployment
resource "azurerm_cognitive_deployment" "embedding" {
  name                 = var.openai_embedding_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.openai_embedding_model
    version = "latest"
  }

  sku {
    name     = "Standard"
    capacity = 10
  }
}
