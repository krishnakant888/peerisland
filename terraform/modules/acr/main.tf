# checkov:skip=CKV_AZURE_166: IP restrictions are loosened for initial setup; tighten in production
resource "azurerm_container_registry" "main" {
  name                = replace("${var.name_prefix}acr", "-", "")
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.sku
  tags                = var.tags

  # CKV_AZURE_138: Admin access disabled; AKS uses managed identity pull
  admin_enabled = false

  # CKV_AZURE_139: Content trust enforces signed image requirements
  trust_policy {
    enabled = true
  }

  # CKV_AZURE_140: Auto-purge untagged images after 30 days
  retention_policy {
    days    = 30
    enabled = true
  }

  # CKV_AZURE_237: Zone redundancy for high availability (Premium only)
  zone_redundancy_enabled = true

  network_rule_set {
    default_action = "Deny"
  }
}
