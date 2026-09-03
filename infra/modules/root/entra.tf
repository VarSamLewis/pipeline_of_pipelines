# Microsoft Entra ID application registration for the app.
#
# Roles are managed here (creator/reviewer/approver/admin) and surface on the
# "roles" claim of the ID token, which auth_service._extract_role_from_claims
# maps back to the local UserRole enum.
resource "azuread_application" "app" {
  display_name = "${local.project_name}-${local.environment}"

  identifier_uris = ["api://${local.resource_prefix}"]

  web {
    redirect_uris = concat(
      ["http://localhost:8000/auth/callback"],
      var.app_public_url != "" ? ["https://${var.app_public_url}/auth/callback"] : [],
    )

    implicit_grant {
      access_token_issuance_enabled = false
      id_token_issuance_enabled     = true
    }
  }

  app_role {
    id                   = uuidv5("6bb84b2f-d8b0-4cef-a1b9-c1b0a2ad90f0", "${local.resource_prefix}-creator")
    allowed_member_types = ["User", "Application"]
    description          = "Can create and submit mapping specifications"
    display_name         = "Creator"
    enabled              = true
    value                = "creator"
  }

  app_role {
    id                   = uuidv5("6bb84b2f-d8b0-4cef-a1b9-c1b0a2ad90f0", "${local.resource_prefix}-reviewer")
    allowed_member_types = ["User"]
    description          = "Can review pending mapping specifications"
    display_name         = "Reviewer"
    enabled              = true
    value                = "reviewer"
  }

  app_role {
    id                   = uuidv5("6bb84b2f-d8b0-4cef-a1b9-c1b0a2ad90f0", "${local.resource_prefix}-approver")
    allowed_member_types = ["User"]
    description          = "Can approve mapping specifications"
    display_name         = "Approver"
    enabled              = true
    value                = "approver"
  }

  app_role {
    id                   = uuidv5("6bb84b2f-d8b0-4cef-a1b9-c1b0a2ad90f0", "${local.resource_prefix}-admin")
    allowed_member_types = ["User"]
    description          = "Full administrative access"
    display_name         = "Admin"
    enabled              = true
    value                = "admin"
  }

  lifecycle {
    ignore_changes = [
      # Allow operators to add prod redirect URIs out of band if needed.
      web[0].redirect_uris,
    ]
  }
}

resource "azuread_application_password" "app" {
  application_id = azuread_application.app.id
  display_name   = "terraform-${local.resource_prefix}"
}

# Wait for the service principal to be visible before assigning roles.
resource "azuread_service_principal" "app" {
  client_id = azuread_application.app.client_id
}

# Flat list of (role, group_object_id) pairs for for_each. Keys are unique per
# (role, group) so multiple groups can share a role.
locals {
  role_assignments = {
    for pair in flatten([
      for role, group_ids in var.role_group_object_ids : [
        for group_id in group_ids : {
          role     = role
          group_id = group_id
        }
      ]
    ]) :
    "${pair.role}:${pair.group_id}" => {
      app_role_id         = azuread_application.app.app_role_ids[pair.role]
      principal_object_id = pair.group_id
    }
  }
}

# Bind Entra groups to the app roles. Which users are in each group is managed
# in the Entra portal, so Terraform only encodes the role -> group mapping.
resource "azuread_app_role_assignment" "role" {
  for_each = local.role_assignments

  app_role_id         = each.value.app_role_id
  principal_object_id = each.value.principal_object_id
  resource_object_id  = azuread_service_principal.app.object_id

  depends_on = [azuread_service_principal.app]
}

# Client ID + secret are surfaced back to the app via Key Vault.
resource "azurerm_key_vault_secret" "entra_client_id" {
  name         = "entra-client-id"
  value        = azuread_application.app.client_id
  key_vault_id = azurerm_key_vault.main.id
}
