output "acr_private_ip" {
  description = "Private IP address assigned to the ACR private endpoint"
  value       = azurerm_private_endpoint.acr.private_service_connection[0].private_ip_address
}

output "keyvault_private_ip" {
  description = "Private IP address assigned to the Key Vault private endpoint"
  value       = azurerm_private_endpoint.keyvault.private_service_connection[0].private_ip_address
}
