resource "azurerm_application_insights" "main" {
  name                = "appi-${local.resource_prefix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  retention_in_days   = var.environment == "prod" ? 90 : 30
  tags                = local.tags
}

# PostgreSQL CPU alarm
resource "azurerm_monitor_metric_alert" "postgres_cpu" {
  name                = "alert-${local.resource_prefix}-pg-cpu"
  resource_group_name = azurerm_resource_group.main.name
  scopes              = [azurerm_postgresql_flexible_server.main.id]
  description         = "PostgreSQL Flexible Server CPU utilization high"

  criteria {
    metric_namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
    metric_name      = "cpu_percent"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 85
  }

  frequency   = "PT5M"
  window_size = "PT30M"

  tags = local.tags
}
