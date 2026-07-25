resource "azuread_application" "app_registration" {
  display_name = var.entra_app_display_name
  sign_in_audience = "AzureADMyOrg"

  web {
    redirect_uris = var.entra_redirect_uris != [] ? var.entra_redirect_uris : [
      "https://${azurerm_container_app.api.latest_revision_fqdn}/auth/callback"
    ]
    home_page_url = "https://${azurerm_container_app.api.latest_revision_fqdn}"
    logout_url    = "https://${azurerm_container_app.api.latest_revision_fqdn}/auth/logout"
  }

  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph
    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d" # User.Read
      type = "Scope"
    }
  }

  app_role {
    allowed_member_types = ["User"]
    description          = "Can create clients, upload files, create mapping specs"
    display_name         = "Creator"
    id                   = "d9c5c5f0-1b3c-4c1e-8f8e-4b3a5c6d7e8f"
    value                = "creator"
    is_enabled           = true
  }

  app_role {
    allowed_member_types = ["User"]
    description          = "Can review mapping proposals and business rules"
    display_name         = "Reviewer"
    id                   = "e8f9a0b1-2c4d-5e6f-9a0b-1c2d3e4f5a6b"
    value                = "reviewer"
    is_enabled           = true
  }

  app_role {
    allowed_member_types = ["User"]
    description          = "Can approve mapping specs and execution runs"
    display_name         = "Approver"
    id                   = "f0a1b2c3-3d4e-6f7a-0b1c-2d3e4f5a6b7c"
    value                = "approver"
    is_enabled           = true
  }

  app_role {
    allowed_member_types = ["User"]
    description          = "Full administrative access"
    display_name         = "Administrator"
    id                   = "a1b2c3d4-4e5f-7a8b-1c2d-3e4f5a6b7c8d"
    value                = "admin"
    is_enabled           = true
  }

  owners = [data.azurerm_client_config.current.object_id]
}

resource "azuread_service_principal" "app_sp" {
  application_id = azuread_application.app_registration.application_id
  app_role_assignment_required = true

  tags = var.tags
}

resource "azuread_service_principal_password" "sp_password" {
  service_principal_id = azuread_service_principal.app_sp.id
  end_date_relative    = "8760h" # 1 year
}

resource "azurerm_key_vault_secret" "azure_openai_key" {
  name         = "azure-openai-key"
  key_vault_id = azurerm_key_vault.main.id
  value        = azurerm_cognitive_account.openai.primary_access_key
}

# Admin user role assignment
resource "azuread_app_role_assignment" "admin_user" {
  app_role_id   = azuread_application.app_registration.app_role[0].id # admin role
  principal_id  = data.azurerm_client_config.current.object_id
  resource_id   = azuread_service_principal.app_sp.object_id
}