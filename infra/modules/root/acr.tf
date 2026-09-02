resource "azurerm_container_registry" "main" {
  name                   = replace(local.resource_prefix, "-", "")
  resource_group_name    = azurerm_resource_group.main.name
  location               = azurerm_resource_group.main.location
  sku                    = "Basic"
  admin_enabled          = false
  anonymous_pull_enabled = false

  tags = local.tags
}
