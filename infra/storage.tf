resource "azurerm_storage_account" "main" {
  name                     = replace("${local.resource_name_prefix}stg", "-", "")
  location                 = var.location
  resource_group_name      = azurerm_resource_group.main.name
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = var.storage_account_kind
  enable_hierarchical_namespace = true
  min_tls_version          = "TLS1_2"
  public_network_access_enabled = var.environment == "prod" ? false : true

  network_rules {
    default_action             = var.environment == "prod" ? "Deny" : "Allow"
    bypass                     = "AzureServices"
    virtual_network_subnet_ids = var.environment == "prod" ? [azurerm_subnet.storage.id] : []
  }

  blob_properties {
    versioning_enabled = true
    change_feed_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  lifecycle_rule {
    name      = "move-to-cool-after-30-days"
    enabled   = true
    type      = "Lifecycle"
    block     = true
    days_after_modification_greater_than = 30
    action {
      base_blob {
        tier_to_cool = true
      }
    }
  }

  tags = var.tags
}

resource "azurerm_storage_container" "raw_files" {
  name                  = "raw-files"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "output_folders" {
  name                  = "output-folders"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "target_schemas" {
  name                  = "target-schemas"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_private_endpoint" "storage_blob" {
  count = var.environment == "prod" ? 1 : 0
  name                = "${local.resource_name_prefix}-stg-pe-blob"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id

  private_service_connection {
    name                           = "storage-blob-connection"
    private_connection_resource_id = azurerm_storage_account.main.id
    is_manual_connection           = false
    subresource_names              = ["blob"]
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.storage_blob.id]
  }
}

resource "azurerm_private_dns_zone" "storage_blob" {
  count = var.environment == "prod" ? 1 : 0
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "storage_blob" {
  count = var.environment == "prod" ? 1 : 0
  name                  = "storage-blob-dns-link"
  private_dns_zone_name = azurerm_private_dns_zone.storage_blob[0].name
  resource_group_name   = azurerm_resource_group.main.name
  virtual_network_id    = azurerm_virtual_network.main.id
  registration_enabled  = false
}