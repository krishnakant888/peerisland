resource "azurerm_key_vault" "main" {
  name                        = "${var.name_prefix}-kv"
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

  # CKV_AZURE_109: Block public network access; use private endpoints
  public_network_access_enabled = false

  # CKV_AZURE_189: Use RBAC instead of access policies for fine-grained control
  enable_rbac_authorization = true

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
