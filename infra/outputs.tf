output "resource_group_name" {
  description = "Azure resource group name"
  value       = azurerm_resource_group.main.name
}

output "container_app_url" {
  description = "Public URL of the API container app"
  value       = "https://${azurerm_container_app.api.latest_revision_fqdn}"
}

output "container_app_fqdn" {
  description = "Container app latest revision FQDN"
  value       = azurerm_container_app.api.latest_revision_fqdn
}

output "acr_login_server" {
  description = "ACR login server"
  value       = azurerm_container_registry.main.login_server
}

output "storage_account_name" {
  description = "Azure storage account name"
  value       = azurerm_storage_account.main.name
}

output "storage_containers" {
  description = "Artifact storage container names"
  value = {
    raw_files      = azurerm_storage_container.raw_files.name
    target_schemas = azurerm_storage_container.target_schemas.name
    output_folders = azurerm_storage_container.output_folders.name
    execution_logs = azurerm_storage_container.execution_logs.name
  }
}

output "key_vault_id" {
  description = "Key Vault resource ID"
  value       = azurerm_key_vault.main.id
}

output "openai_endpoint" {
  description = "Azure OpenAI endpoint"
  value       = "https://${azurerm_cognitive_account.openai.custom_subdomain_name}.openai.azure.com"
}

output "postgres_fqdn" {
  description = "PostgreSQL Flexible Server FQDN"
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "entra_application_id" {
  description = "Entra ID application (client) ID"
  value       = azuread_application.app.client_id
}

output "entra_application_object_id" {
  description = "Entra ID application object ID"
  value       = azuread_application.app.object_id
}
