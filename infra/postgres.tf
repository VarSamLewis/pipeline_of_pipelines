resource "azurerm_postgresql_flexible_server" "main" {
  name                = "pgs-${local.resource_prefix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  version                      = var.database_version
  sku_name                     = var.database_sku
  storage_mb                   = var.database_storage_mb
  administrator_login          = var.database_username
  administrator_password       = var.database_password
  backup_retention_days        = var.environment == "prod" ? 30 : 7
  geo_redundant_backup_enabled = var.environment == "prod"

  # Private networking: delegated subnet + private DNS
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false

  storage_tier = "P20"

  tags = local.tags
}

resource "azurerm_postgresql_flexible_server_database" "main" {
  name      = "pipeline"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Enable pgvector via server configuration (no null_resource psql hack needed)
resource "azurerm_postgresql_flexible_server_configuration" "pgvector" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "VECTOR"
}
