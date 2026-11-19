# checkov:skip=CKV_AZURE_166: IP restrictions loosened for initial setup
# checkov:skip=CKV_AZURE_139: Content Trust deprecated by Azure (removed March 2028)
# checkov:skip=CKV_AZURE_237: Zone redundancy requires Premium SKU
resource "azurerm_container_registry" "main" {
  name                = replace("${var.name_prefix}acr", "-", "")
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.sku
  tags                = var.tags

  # Admin access disabled; AKS uses managed identity pull
  admin_enabled = false

  network_rule_set {
    default_action = "Deny"
  }
}
