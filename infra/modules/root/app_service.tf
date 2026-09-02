locals {
  # Full connection string with password embedded is stored as a Key Vault
  # secret so no credential ever appears in state or inline env.
  database_url = "postgresql+psycopg://${var.database_username}:${urlencode(var.database_password)}@${azurerm_postgresql_flexible_server.main.fqdn}:5432/pipeline?sslmode=require"
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "database-url"
  value        = local.database_url
  key_vault_id = azurerm_key_vault.main.id
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-${local.resource_prefix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  retention_in_days   = var.environment == "prod" ? 30 : 14
  tags                = local.tags
}

# App Service Plan (Linux) hosting the FastAPI container.
resource "azurerm_service_plan" "main" {
  name                = "plan-${local.resource_prefix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = var.app_service_sku
  tags                = local.tags
}

resource "azurerm_linux_web_app" "main" {
  name                = "app-${local.resource_prefix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  service_plan_id     = azurerm_service_plan.main.id
  https_only          = true
  tags                = local.tags

  identity {
    type = "SystemAssigned"
  }

  site_config {
    linux_fx_version  = "DOCKER|${azurerm_container_registry.main.login_server}/pipeline-api:${var.container_image_tag}"
    health_check_path = "/health"
    always_on         = true
  }

  # Plain variables referenced inline; secrets via Key Vault references so no
  # credential ever lands in state.
  app_settings = {
    ENTRA_TENANT_ID                 = var.entra_tenant_id
    ENTRA_REDIRECT_URI              = "https://${azurerm_linux_web_app.main.default_hostname}/auth/callback"
    ENTRA_CLIENT_ID                 = azuread_application.app.client_id
    AZURE_OPENAI_ENDPOINT           = "https://${azurerm_cognitive_account.openai.custom_subdomain_name}.openai.azure.com"
    AZURE_OPENAI_API_VERSION        = var.openai_api_version
    MAPPING_MODEL                   = var.openai_chat_model
    CODEGEN_MODEL                   = var.openai_chat_model
    EMBEDDING_MODEL                 = var.openai_embedding_model
    SESSION_COOKIE_SECURE           = "true"
    ENTRA_CLIENT_SECRET             = "@Microsoft.KeyVault(SecretUri=${azurerm_key_vault_secret.entra_client_secret.id})"
    AZURE_OPENAI_API_KEY            = "@Microsoft.KeyVault(SecretUri=${azurerm_key_vault_secret.openai_api_key.id})"
    SESSION_SECRET_KEY              = "@Microsoft.KeyVault(SecretUri=${azurerm_key_vault_secret.session_secret.id})"
    DATABASE_URL                    = "@Microsoft.KeyVault(SecretUri=${azurerm_key_vault_secret.database_url.id})"
    AZURE_STORAGE_CONNECTION_STRING = "@Microsoft.KeyVault(SecretUri=${azurerm_key_vault_secret.storage_connection_string.id})"
  }
}

# Regional VNet integration so the app can reach the private PostgreSQL server.
resource "azurerm_app_service_virtual_network_swift_connection" "main" {
  app_service_id = azurerm_linux_web_app.main.id
  subnet_id      = azurerm_subnet.app_service.id
}
