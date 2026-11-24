# ── Key Vault module outputs ──────────────────────────────────────────────────
# id        → used by root main.tf for KV secrets and by privateendpoints module
#              to create the private endpoint connection
# name      → needed by bootstrap.py phase 6 to write secrets via az keyvault
# vault_uri → "https://<name>.vault.azure.net/" — used in SecretProviderClass
#              and surfaced to root outputs for operator reference
#
output "id" {
  value = azurerm_key_vault.main.id
}

output "name" {
  value = azurerm_key_vault.main.name
}

output "vault_uri" {
  value = azurerm_key_vault.main.vault_uri
}
