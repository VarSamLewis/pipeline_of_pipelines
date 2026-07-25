output "resource_group_name" {
  description = "Name of the resource group"
  value       = azurerm_resource_group.main.name
}

output "resource_group_id" {
  description = "ID of the resource group"
  value       = azurerm_resource_group.main.id
}

output "vnet_id" {
  description = "Virtual Network ID"
  value       = azurerm_virtual_network.main.id
}

output "vnet_name" {
  description = "Virtual Network name"
  value       = azurerm_virtual_network.main.name
}

output "container_apps_subnet_id" {
  description = "Container Apps subnet ID"
  value       = azurerm_subnet.container_apps.id
}

output "postgres_server_fqdn" {
  description = "PostgreSQL Flexible Server FQDN"
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "postgres_database_name" {
  description = "PostgreSQL database name"
  value       = azurerm_postgresql_flexible_server_database.main.name
}

output "postgres_admin_username" {
  description = "PostgreSQL admin username"
  value       = azurerm_postgresql_flexible_server.main.administrator_login
}

output "postgres_admin_password" {
  description = "PostgreSQL admin password (sensitive)"
  value       = var.postgres_admin_password
  sensitive   = true
}

output "storage_account_name" {
  description = "Storage account name"
  value       = azurerm_storage_account.main.name
}

output "storage_account_primary_endpoint" {
  description = "Storage account primary blob endpoint"
  value       = azurerm_storage_account.main.primary_blob_endpoint
}

output "storage_raw_files_container" {
  description = "Raw files container name"
  value       = azurerm_storage_container.raw_files.name
}

output "storage_output_folders_container" {
  description = "Output folders container name"
  value       = azurerm_storage_container.output_folders.name
}

output "storage_target_schemas_container" {
  description = "Target schemas container name"
  value       = azurerm_storage_container.target_schemas.name
}

output "acr_name" {
  description = "Container Registry name"
  value       = azurerm_container_registry.main.name
}

output "acr_login_server" {
  description = "Container Registry login server"
  value       = azurerm_container_registry.main.login_server
}

output "aca_environment_id" {
  description = "Container Apps Environment ID"
  value       = azurerm_container_app_environment.main.id
}

output "aca_environment_name" {
  description = "Container Apps Environment name"
  value       = azurerm_container_app_environment.main.name
}

output "api_container_app_fqdn" {
  description = "API Container App FQDN"
  value       = azurerm_container_app.api.latest_revision_fqdn
}

output "pipeline_job_container_app_name" {
  description = "Pipeline Job Container App name"
  value       = azurerm_container_app.pipeline_job.name
}

output "key_vault_name" {
  description = "Key Vault name"
  value       = azurerm_key_vault.main.name
}

output "key_vault_uri" {
  description = "Key Vault URI"
  value       = azurerm_key_vault.main.vault_uri
}

output "managed_identity_client_id" {
  description = "User Assigned Managed Identity Client ID"
  value       = azurerm_user_assigned_identity.main.client_id
}

output "managed_identity_principal_id" {
  description = "User Assigned Managed Identity Principal ID"
  value       = azurerm_user_assigned_identity.main.principal_id
}

output "entra_app_client_id" {
  description = "Entra ID App Registration Client ID"
  value       = azuread_application.main.client_id
}

output "entra_app_object_id" {
  description = "Entra ID App Registration Object ID"
  value       = azuread_application.main.object_id
}

output "ai_services_endpoint" {
  description = "Azure AI Services endpoint"
  value       = var.enable_ai_services ? azurerm_cognitive_account.main.endpoint : ""
}

output "ai_services_key" {
  description = "Azure AI Services primary key (sensitive)"
  value       = var.enable_ai_services ? azurerm_cognitive_account.main.primary_access_key : ""
  sensitive   = true
}

output "log_analytics_workspace_id" {
  description = "Log Analytics Workspace ID"
  value       = var.enable_monitoring ? azurerm_log_analytics_workspace.main.id : ""
}

output "log_analytics_workspace_key" {
  description = "Log Analytics Workspace Primary Key (sensitive)"
  value       = var.enable_monitoring ? azurerm_log_analytics_workspace.main.primary_shared_key : ""
  sensitive   = true
}