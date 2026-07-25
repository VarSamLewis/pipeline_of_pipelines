resource "azurerm_container_app_environment" "main" {
  name                = local.aca_env_name
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  zone_redundant      = var.environment == "prod"

  infrastructure {
    subnet_id = azurerm_subnet.container_apps.id
  }

  tags = var.tags
}

# API Container App
resource "azurerm_container_app" "api" {
  name                = "${local.resource_name_prefix}-api"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  managed_identities {
    system_assigned = false
    user_assigned   = [azurerm_user_assigned_identity.container_apps.id]
  }

  template {
    container {
      name   = "api"
      image  = "${azurerm_container_registry.main.login_server}/pipeline-api:latest"
      cpu    = 0.5
      memory = "1Gi"

      env = [
        { name = "DATABASE_URL", value = azurerm_key_vault_secret.database_url.value },
        { name = "SESSION_SECRET_KEY", value = azurerm_key_vault_secret.session_secret.value },
        { name = "ENTRA_CLIENT_ID", value = azurerm_key_vault_secret.entra_client_id.value },
        { name = "ENTRA_CLIENT_SECRET", value = azurerm_key_vault_secret.entra_client_secret.value },
        { name = "ENTRA_TENANT_ID", value = azurerm_key_vault_secret.entra_tenant_id.value },
        { name = "AZURE_OPENAI_ENDPOINT", value = azurerm_key_vault_secret.azure_openai_endpoint.value },
        { name = "AZURE_OPENAI_KEY", value = azurerm_key_vault_secret.azure_openai_key.value },
        { name = "STORAGE_ACCOUNT_URL", value = azurerm_key_vault_secret.storage_account_url.value },
        { name = "OPENAI_API_KEY", value = azurerm_key_vault_secret.azure_openai_key.value },
        { name = "OPENAI_BASE_URL", value = "${azurerm_key_vault_secret.azure_openai_endpoint.value}openai/deployments/${var.openai_deployment_name}" },
        { name = "HTTPS_ONLY", value = "true" },
        { name = "ENVIRONMENT", value = var.environment },
      ]

      liveness_probe {
        http_get {
          path = "/health"
          port = 8000
        }
        initial_delay_seconds = 30
        period_seconds        = 10
      }

      readiness_probe {
        http_get {
          path = "/health"
          port = 8000
        }
        initial_delay_seconds = 5
        period_seconds        = 5
      }
    }

    scale {
      min_replicas = var.environment == "prod" ? 1 : 0
      max_replicas = 10

      rule {
        name = "http-rule"
        http {
          metadata = {
            concurrent_requests = "50"
          }
        }
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"
    allow_insecure   = false

    traffic_weight {
      latest_revision = true
      weight          = 100
    }
  }

  tags = var.tags
}

# Pipeline Job Container App
resource "azurerm_container_app" "pipeline_job" {
  name                = "${local.resource_name_prefix}-pipeline-job"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  managed_identities {
    system_assigned = false
    user_assigned   = [azurerm_user_assigned_identity.container_apps.id]
  }

  configuration {
    trigger_type = "Event"
    event_trigger {
      trigger_name = "http"
      event_type   = "http"
    }
  }

  template {
    container {
      name  = "pipeline-worker"
      image = "${azurerm_container_registry.main.login_server}/pipeline-worker:latest"
      cpu   = 1.0
      memory = "2Gi"

      env = [
        { name = "DATABASE_URL", value = azurerm_key_vault_secret.database_url.value },
        { name = "AZURE_OPENAI_ENDPOINT", value = azurerm_key_vault_secret.azure_openai_endpoint.value },
        { name = "AZURE_OPENAI_KEY", value = azurerm_key_vault_secret.azure_openai_key.value },
        { name = "STORAGE_ACCOUNT_URL", value = azurerm_key_vault_secret.storage_account_url.value },
        { name = "OPENAI_API_KEY", value = azurerm_key_vault_secret.azure_openai_key.value },
        { name = "OPENAI_BASE_URL", value = "${azurerm_key_vault_secret.azure_openai_endpoint.value}openai/deployments/${var.openai_deployment_name}" },
        { name = "ENVIRONMENT", value = var.environment },
      ]
    }

    scale {
      min_replicas = 0
      max_replicas = 20

      rule {
        name = "http-rule"
        http {
          metadata = {
            concurrent_requests = "1"
          }
        }
      }
    }
  }

  tags = var.tags
}

# API health endpoint route
resource "azurerm_container_app_revision" "api_latest" {
  container_app_name = azurerm_container_app.api.name
  resource_group_name = azurerm_resource_group.main.name
  revision_suffix = "latest"
}