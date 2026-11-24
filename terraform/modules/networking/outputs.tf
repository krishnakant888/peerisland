# ── Networking module outputs ─────────────────────────────────────────────────
# vnet_id      → AKS module (Network Contributor role) + privateendpoints module
# vnet_name    → referenced in operator docs; not used by other modules
# aks_subnet_id  → AKS module: nodes are placed in this subnet (Azure CNI)
# appgw_subnet_id → reserved; would be used by an Application Gateway module
# pe_subnet_id   → privateendpoints module: PE NICs attach here
#
output "vnet_id" {
  value = azurerm_virtual_network.main.id
}

output "vnet_name" {
  value = azurerm_virtual_network.main.name
}

output "aks_subnet_id" {
  value = azurerm_subnet.aks.id
}

output "appgw_subnet_id" {
  value = azurerm_subnet.appgw.id
}

output "pe_subnet_id" {
  value = azurerm_subnet.pe.id
}
