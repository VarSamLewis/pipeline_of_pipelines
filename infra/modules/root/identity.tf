# The App Service uses a system-assigned managed identity. Storage uses a
# connection string and OpenAI uses an API key (both from Key Vault), so no
# role assignments are needed for those. The only role grant is for pulling
# images from ACR.

resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_linux_web_app.main.identity[0].principal_id

  depends_on = [azurerm_linux_web_app.main]
}
