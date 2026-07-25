resource "azurerm_user_assigned_identity" "container_apps" {
  name                = "${local.resource_name_prefix}-uami"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_role_assignment" "container_apps_storage" {
  scope                = azurerm_storage_account.main.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.container_apps.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "container_apps_keyvault" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.container_apps.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "container_apps_postgres" {
  scope                = azurerm_postgresql_flexible_server.main.id
  role_definition_name = "PostgreSQL Flexible Server Contributor"
  principal_id         = azurerm_user_assigned_identity.container_apps.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "container_apps_cognitive" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_user_assigned_identity.container_apps.principal_id
  principal_type       = "ServicePrincipal"
}