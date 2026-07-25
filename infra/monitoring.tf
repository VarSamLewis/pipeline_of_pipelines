resource "azurerm_log_analytics_workspace" "main" {
  name                = "${local.resource_name_prefix}-law"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_analytics_retention
  tags                = var.tags
}

resource "azurerm_application_insights" "main" {
  name                = "${local.resource_name_prefix}-appinsights"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.main.id
  tags                = var.tags
}

resource "azurerm_monitor_diagnostic_setting" "container_apps_env" {
  name                       = "container-apps-diagnostics"
  target_resource_id         = azurerm_container_app_environment.main.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  log {
    category = "ContainerAppConsoleLogs"
    enabled  = true
    retention_policy {
      enabled = true
      days    = var.log_analytics_retention
    }
  }

  log {
    category = "ContainerAppSystemLogs"
    enabled  = true
    retention_policy {
      enabled = true
      days    = var.log_analytics_retention
    }
  }

  metric {
    category = "AllMetrics"
    enabled  = true
    retention_policy {
      enabled = true
      days    = var.log_analytics_retention
    }
  }
}