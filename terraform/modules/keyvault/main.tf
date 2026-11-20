resource "random_string" "kv_suffix" {
  length  = 4
  upper   = false
  special = false
}

resource "azurerm_key_vault" "main" {
  # suffix: use override from tfvars if set (for state-loss recovery), else random on first create
  name = "${var.name_prefix}-kv-${var.key_vault_suffix != "" ? var.key_vault_suffix : random_string.kv_suffix.result}"
  location                    = var.location
  resource_group_name         = var.resource_group_name
  enabled_for_disk_encryption = true
  tenant_id                   = var.tenant_id
  sku_name                    = "standard"
  tags                        = var.tags

  # CKV_AZURE_42: Soft delete always enabled in AzureRM 3.x+
  soft_delete_retention_days = 90

  # CKV_AZURE_110: Prevents permanent deletion of vault
  purge_protection_enabled = true

  # Public access is restricted to deployer IPs via network_acls below.
  # Must be true so the deployer can seed secrets from their workstation.
  public_network_access_enabled = true

  # CKV_AZURE_189: Use RBAC instead of access policies for fine-grained control
  rbac_authorization_enabled = true

  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"

    # Allow CI/CD pipeline or operator workstation IPs during terraform apply.
    # The private endpoint handles runtime access from AKS — these IPs are only
    # needed for the deployer to call az keyvault secret set / terraform plan.
    ip_rules = var.deployer_ips
  }
}

# Grant the deploying principal Key Vault Administrator (can be removed post-setup)
resource "azurerm_role_assignment" "kv_admin" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = var.object_id
}
