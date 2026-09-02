resource "azurerm_key_vault" "main" {
  name                       = "kv-${replace(local.resource_prefix, "-", "")}"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  soft_delete_retention_days = 7
  purge_protection_enabled   = true
  rbac_authorization_enabled = false

  tags = local.tags
}

# --- Secrets (referenced by the Container App via Key Vault secret references) ---

resource "azurerm_key_vault_secret" "database_password" {
  name         = "database-password"
  value        = var.database_password
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "session_secret" {
  name         = "session-secret"
  value        = var.session_secret_key
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "openai_api_key" {
  name         = "azure-openai-api-key"
  value        = var.azure_openai_api_key
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "entra_client_secret" {
  name         = "entra-client-secret"
  value        = var.entra_client_secret
  key_vault_id = azurerm_key_vault.main.id
}

# Storage accessed via connection string (held in Key Vault, not state).
resource "azurerm_key_vault_secret" "storage_connection_string" {
  name         = "azure-storage-connection-string"
  value        = azurerm_storage_account.main.primary_connection_string
  key_vault_id = azurerm_key_vault.main.id
}

# Allow the container app's system-assigned identity to read secrets at runtime.
resource "azurerm_key_vault_access_policy" "container_app" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = var.tenant_id
  object_id    = azurerm_container_app.api.identity[0].principal_id

  secret_permissions = ["Get", "List"]

  depends_on = [azurerm_container_app.api]
}
