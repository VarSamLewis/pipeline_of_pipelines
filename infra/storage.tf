resource "azurerm_storage_account" "main" {
  name                       = "${replace(local.resource_prefix, "-", "")}storage"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  account_tier               = "Standard"
  account_replication_type   = "LRS"
  min_tls_version            = "TLS1_2"
  https_traffic_only_enabled = true

  blob_properties {
    versioning_enabled = true
  }

  tags = local.tags
}

# Artifact containers. Names must match the defaults used by
# AzureArtifactStore in backend/src/artifact_store.py.
resource "azurerm_storage_container" "raw_files" {
  name                  = "raw-files"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "target_schemas" {
  name                  = "target-schemas"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "output_folders" {
  name                  = "output-folders"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "execution_logs" {
  name                  = "execution-logs"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}
