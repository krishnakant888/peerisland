# ── ACR Private Endpoint ─────────────────────────────────────────────────────
# Allows AKS nodes to pull images via private IP instead of the public endpoint.
# ACR has default_action=Deny, so without this the kubelet cannot pull images.
resource "azurerm_private_endpoint" "acr" {
  name                = "${var.name_prefix}-acr-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.name_prefix}-acr-psc"
    private_connection_resource_id = var.acr_id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "acr-dns-group"
    private_dns_zone_ids = [azurerm_private_dns_zone.acr.id]
  }
}

resource "azurerm_private_dns_zone" "acr" {
  name                = "privatelink.azurecr.io"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# Links the ACR private DNS zone to the VNet so AKS nodes resolve
# <acr>.azurecr.io → private IP instead of the public endpoint.
resource "azurerm_private_dns_zone_virtual_network_link" "acr" {
  name                  = "${var.name_prefix}-acr-dns-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.acr.name
  virtual_network_id    = var.vnet_id
  registration_enabled  = false
  tags                  = var.tags
}

# ── Key Vault Private Endpoint ───────────────────────────────────────────────
# Allows AKS CSI driver to read secrets via private IP.
# Key Vault has public_network_access_enabled=false, so this is mandatory.
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
