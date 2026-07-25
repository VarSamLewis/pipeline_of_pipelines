resource "azurerm_postgresql_flexible_server" "main" {
  name                         = "${local.resource_name_prefix}-pg"
  location                     = var.location
  resource_group_name          = azurerm_resource_group.main.name
  administrator_login          = var.postgres_admin_username
  administrator_password       = var.postgres_admin_password
  version                      = var.postgres_version
  zone                         = var.environment == "prod" ? "1" : null
  storage_mb                   = var.postgres_storage_gb * 1024
  backup_retention_days        = var.environment == "prod" ? 30 : 7
  geo_redundant_backup_enabled = var.environment == "prod"
  high_availability {
    mode             = var.environment == "prod" ? "ZoneRedundant" : "Disabled"
    standby_availability_zone = var.environment == "prod" ? "2" : null
  }

  sku {
    name   = var.postgres_sku.name
    tier   = var.postgres_sku.tier
    family = var.postgres_sku.family
  }

  network {
    subnet_id                    = azurerm_subnet.postgres.id
    delegated_subnet_enabled     = true
    private_dns_zone_id          = azurerm_private_dns_zone.postgres.id
  }

  tags = var.tags
}

resource "azurerm_postgresql_flexible_server_database" "main" {
  name      = "pipeline"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_configuration" "pgvector" {
  server_id  = azurerm_postgresql_flexible_server.main.id
  name       = "azure.extensions"
  value      = "vector"
}

resource "azurerm_private_dns_zone" "postgres" {
  name                = "privatelink.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "pg-dns-link"
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  resource_group_name   = azurerm_resource_group.main.name
  virtual_network_id    = azurerm_virtual_network.main.id
  registration_enabled  = false
}

resource "azurerm_private_endpoint" "postgres" {
  name                = "${local.resource_name_prefix}-pg-pe"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.postgres.id

  private_service_connection {
    name                           = "postgres-connection"
    private_connection_resource_id = azurerm_postgresql_flexible_server.main.id
    is_manual_connection           = false
    subresource_names              = ["postgresql"]
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.postgres.id]
  }
}