resource "azurerm_key_vault" "main" {
  name                        = "${local.resource_name_prefix}-kv"
  location                    = var.location
  resource_group_name         = azurerm_resource_group.main.name
  tenant_id                   = data.azurerm_client_config.current.tenant_id
  sku_name                    = "standard"
  purge_protection_enabled    = var.environment == "prod"
  soft_delete_retention_days  = 7

  network_acls {
    default_action             = "Allow"
    bypass                     = "AzureServices"
    virtual_network_subnet_ids = var.environment == "prod" ? [azurerm_subnet.keyvault.id] : []
  }

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = [
      "Get", "List", "Set", "Delete", "Recover", "Backup", "Restore", "Purge"
    ]
    key_permissions = [
      "Get", "List", "Create", "Delete", "Recover", "Backup", "Restore", "Purge", "Decrypt", "Encrypt", "UnwrapKey", "WrapKey", "Verify", "Sign", "Import"
    ]
    certificate_permissions = [
      "Get", "List", "Create", "Delete", "Recover", "Backup", "Restore", "Purge", "Import", "ManageContacts", "ManageIssuers", "GetIssuers", "ListIssuers"
    ]
    storage_permissions = [
      "Get", "List", "Create", "Delete", "Recover", "Backup", "Restore", "Purge", "RegenerateKey", "Set", "Update"
    ]
  }

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = azurerm_user_assigned_identity.container_apps.principal_id

    secret_permissions = [
      "Get", "List", "Set", "Delete"
    ]
  }

  tags = var.tags
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "database-url"
  value        = "postgresql+psycopg://${azurerm_postgresql_flexible_server.main.administrator_login}:${azurerm_postgresql_flexible_server.main.administrator_password}@${azurerm_postgresql_flexible_server.main.fqdn}:5432/${azurerm_postgresql_flexible_database.main.name}"
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "session_secret" {
  name         = "session-secret-key"
  value        = random_password.session_secret.result
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "entra_client_id" {
  name         = "entra-client-id"
  value        = azuread_application.pipeline_of_pipelines.client_id
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "entra_client_secret" {
  name         = "entra-client-secret"
  value        = azuread_application_password.pipeline_of_pipelines.value
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "entra_tenant_id" {
  name         = "entra-tenant-id"
  value        = data.azurerm_client_config.current.tenant_id
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "azure_openai_endpoint" {
  name         = "azure-openai-endpoint"
  value        = azurerm_cognitive_account.openai.endpoint
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "azure_openai_key" {
  name         = "azure-openai-key"
  value        = azurerm_cognitive_account.openai.primary_access_key
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_key_vault_secret" "storage_account_url" {
  name         = "storage-account-url"
  value        = azurerm_storage_account.main.primary_blob_endpoint
  key_vault_id = azurerm_key_vault.main.id
}

resource "random_password" "session_secret" {
  length  = 48
  special = false
}

data "azurerm_client_config" "current" {}