# ── Private Endpoints module outputs ─────────────────────────────────────────
# keyvault_private_ip: the IP address within pe-subnet that KV traffic resolves to.
#   Useful for debugging ("does the DNS zone resolve to the right IP?").
#   Not consumed by other Terraform modules — purely informational.
#
output "keyvault_private_ip" {
  description = "Private IP address assigned to the Key Vault private endpoint"
  value       = azurerm_private_endpoint.keyvault.private_service_connection[0].private_ip_address
}
