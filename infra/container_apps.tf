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

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${local.resource_prefix}"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id   = azurerm_subnet.container_apps.id
  tags                       = local.tags
}

resource "azurerm_container_app" "api" {
  name                         = "ca-${local.resource_prefix}-api"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  tags                         = local.tags

  identity {
    type = "SystemAssigned"
  }

  revision_mode = "Single"

  # Secrets at root level, pulled from Key Vault via the system-assigned
  # identity (no explicit identity attribute needed for system-assigned).
  secret {
    name                = "entra-client-secret"
    key_vault_secret_id = azurerm_key_vault_secret.entra_client_secret.id
  }

  secret {
    name                = "azure-openai-api-key"
    key_vault_secret_id = azurerm_key_vault_secret.openai_api_key.id
  }

  secret {
    name                = "session-secret"
    key_vault_secret_id = azurerm_key_vault_secret.session_secret.id
  }

  secret {
    name                = "database-url"
    key_vault_secret_id = azurerm_key_vault_secret.database_url.id
  }

  secret {
    name                = "azure-storage-connection-string"
    key_vault_secret_id = azurerm_key_vault_secret.storage_connection_string.id
  }

  template {
    container {
      name   = "api"
      image  = "${azurerm_container_registry.main.login_server}/pipeline-api:${var.container_image_tag}"
      cpu    = 1
      memory = "2Gi"

      env {
        name  = "ENTRA_TENANT_ID"
        value = var.entra_tenant_id
      }

      env {
        name  = "ENTRA_REDIRECT_URI"
        value = "https://${azurerm_container_app.api.latest_revision_fqdn}/auth/callback"
      }

      env {
        name  = "ENTRA_CLIENT_ID"
        value = azuread_application.app.client_id
      }

      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = "https://${azurerm_cognitive_account.openai.custom_subdomain_name}.openai.azure.com"
      }

      env {
        name  = "AZURE_OPENAI_API_VERSION"
        value = var.openai_api_version
      }

      env {
        name  = "MAPPING_MODEL"
        value = var.openai_chat_model
      }

      env {
        name  = "CODEGEN_MODEL"
        value = var.openai_chat_model
      }

      env {
        name  = "EMBEDDING_MODEL"
        value = var.openai_embedding_model
      }

      env {
        name  = "SESSION_COOKIE_SECURE"
        value = "true"
      }

      env {
        name        = "ENTRA_CLIENT_SECRET"
        secret_name = "entra-client-secret"
      }

      env {
        name        = "AZURE_OPENAI_API_KEY"
        secret_name = "azure-openai-api-key"
      }

      env {
        name        = "SESSION_SECRET_KEY"
        secret_name = "session-secret"
      }

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }

      env {
        name        = "AZURE_STORAGE_CONNECTION_STRING"
        secret_name = "azure-storage-connection-string"
      }

      liveness_probe {
        port          = 8000
        transport     = "HTTP"
        path          = "/health"
        initial_delay = 10
      }

      readiness_probe {
        port          = 8000
        transport     = "HTTP"
        path          = "/health"
        initial_delay = 5
      }
    }
  }

  ingress {
    target_port      = 8000
    external_enabled = true
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
    ]
  }
}
