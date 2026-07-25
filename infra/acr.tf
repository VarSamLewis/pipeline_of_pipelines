resource "azurerm_container_registry" "main" {
  name                = replace("${local.resource_name_prefix}acr", "-", "")
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = var.acr_sku
  admin_enabled       = false

  public_network_access_enabled = var.environment == "prod" ? false : true

  network_rules {
    default_action             = var.environment == "prod" ? "Deny" : "Allow"
    virtual_network_subnet_ids = var.environment == "prod" ? [azurerm_subnet.acr.id] : []
  }

  retention_policy {
    days        = 30
    enabled     = true
    last_pushed = false
  }

  tags = var.tags
}

resource "azurerm_role_assignment" "acr_pull_container_apps" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.container_apps.principal_id
}

resource "azurerm_private_endpoint" "acr" {
  count = var.environment == "prod" ? 1 : 0
  name                = "${local.resource_name_prefix}-acr-pe"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.acr.id

  private_service_connection {
    name                           = "acr-connection"
    private_connection_resource_id = azurerm_container_registry.main.id
    is_manual_connection           = false
    subresource_names              = ["registry"]
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.acr.id]
  }
}

resource "azurerm_private_dns_zone" "acr" {
  count = var.environment == "prod" ? 1 : 0
  name                = "privatelink.azurecr.io"
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "acr" {
  count = var.environment == "prod" ? 1 : 0
  name                  = "acr-dns-link"
  private_dns_zone_name = azurerm_private_dns_zone.acr[0].name
  resource_group_name   = azurerm_resource_group.main.name
  virtual_network_id    = azurerm_virtual_network.main.id
  registration_enabled  = false
}