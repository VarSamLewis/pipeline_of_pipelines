resource "azurerm_cognitive_account" "openai" {
  count = var.enable_ai_services ? 1 : 0

  name                = "${local.resource_name_prefix}-openai"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  kind                = "OpenAI"
  sku_name            = var.ai_services_sku
  tags                = var.tags

  custom_subdomain_name = "${local.resource_name_prefix}-openai"

  network_acls {
    default_action = var.environment == "prod" ? "Deny" : "Allow"
    bypass         = "AzureServices"
    virtual_networks = var.environment == "prod" ? [azurerm_subnet.ai_services.id] : []
  }

  public_network_access_enabled = var.environment == "prod" ? false : true

  model {
    format  = "OpenAI"
    name    = var.openai_deployment_name
    version = "2024-05-13"
    sku_name = "Standard"
    sku_capacity = 10
  }

  model {
    format  = "OpenAI"
    name    = var.embedding_deployment_name
    version = "1"
    sku_name = "Standard"
    sku_capacity = 10
  }
}

resource "azurerm_private_endpoint" "openai" {
  count = var.environment == "prod" && var.enable_ai_services ? 1 : 0

  name                = "${local.resource_name_prefix}-openai-pe"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.ai_services.id

  private_service_connection {
    name                           = "openai-connection"
    private_connection_resource_id = azurerm_cognitive_account.openai[0].id
    is_manual_connection           = false
    subresource_names              = ["account"]
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.openai.id]
  }
}

resource "azurerm_private_dns_zone" "openai" {
  count = var.environment == "prod" && var.enable_ai_services ? 1 : 0
  name                = "privatelink.cognitiveservices.azure.com"
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "openai" {
  count = var.environment == "prod" && var.enable_ai_services ? 1 : 0
  name                  = "openai-dns-link"
  private_dns_zone_name = azurerm_private_dns_zone.openai[0].name
  resource_group_name   = azurerm_resource_group.main.name
  virtual_network_id    = azurerm_virtual_network.main.id
  registration_enabled  = false
}