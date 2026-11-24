# ── Private Endpoints module ──────────────────────────────────────────────────
#
# PURPOSE: Gives cluster workloads private (VNet-internal) access to Key Vault.
#   Without this, AKS CSI secrets driver requests would go over the public internet
#   and would be blocked by KV's public_network_access_enabled = false setting.
#
# HOW IT WORKS (end-to-end):
#   1. Private endpoint creates a NIC in pe-subnet with a private IP (e.g. 10.242.0.5)
#   2. Private DNS zone "privatelink.vaultcore.azure.net" is linked to the VNet
#   3. VNet DNS resolves "peerisland-prod-kv.vault.azure.net"
#      → "peerisland-prod-kv.privatelink.vaultcore.azure.net" → 10.242.0.5
#   4. AKS CSI driver connects to that private IP — traffic never leaves the VNet
#
# WHY registration_enabled = false on the DNS zone link?
#   registration_enabled = true auto-registers VM hostnames in the zone.
#   That's for VM DNS auto-discovery — we don't want VMs registering themselves
#   in the Key Vault DNS zone.  We manage records manually via the endpoint group.
#
# NOTE: There is no ACR private endpoint in this module.  ACR currently has
#   public access enabled (CKV_AZURE_139 skipped in the ACR module).  Add an
#   ACR private endpoint here when you move to Premium SKU.
#
# ── Key Vault Private Endpoint ───────────────────────────────────────────────
# AKS CSI driver reads secrets via private IP.
# Key Vault has public_network_access_enabled=false so this is mandatory.
resource "azurerm_private_endpoint" "keyvault" {
  name                = "${var.name_prefix}-kv-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.name_prefix}-kv-psc"
    private_connection_resource_id = var.key_vault_id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "kv-dns-group"
    private_dns_zone_ids = [azurerm_private_dns_zone.keyvault.id]
  }
}

resource "azurerm_private_dns_zone" "keyvault" {
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "keyvault" {
  name                  = "${var.name_prefix}-kv-dns-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.keyvault.name
  virtual_network_id    = var.vnet_id
  registration_enabled  = false
  tags                  = var.tags
}
