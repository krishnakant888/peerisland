# ── ACR module outputs ────────────────────────────────────────────────────────
# id           → used by AKS module for AcrPull role assignment scope
#                and by privateendpoints module for the PE connection resource ID
# name         → passed to GitHub Actions secret ACR_NAME
# login_server → "peerislandprodacr.azurecr.io" — image base URL in CI/CD
#
output "id" {
  value = azurerm_container_registry.main.id
}

output "name" {
  value = azurerm_container_registry.main.name
}

output "login_server" {
  value = azurerm_container_registry.main.login_server
}
