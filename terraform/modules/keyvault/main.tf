# ── Key Vault module ──────────────────────────────────────────────────────────
#
# PURPOSE:
#   Stores Grafana admin credentials (username + password) so they never appear
#   in Git or Terraform state. The credentials flow:
#     terraform.tfvars (local, gitignored) → Key Vault → CSI driver → k8s Secret → Grafana
#
# RBAC vs Access Policies:
#   rbac_authorization_enabled = true: use Azure RBAC instead of KV access policies.
#   RBAC is the modern approach — finer-grained (can restrict to specific secrets),
#   integrates with Azure AD Conditional Access, and shows in IAM audit logs.
#
# NAMING:
#   Key Vault names must be globally unique. We append a random suffix to avoid
#   collision. After state loss (e.g. terraform import recovery), set var.key_vault_suffix
#   to pin the same suffix so Terraform finds the existing vault instead of trying to
#   create a new one with a different suffix.
#
# NETWORK ACCESS:
#   default_action = "Deny": only allowed IPs can reach the KV data plane.
#   At deploy time, the deployer's workstation IP is in var.deployer_ips.
#   At runtime, the AKS CSI driver reaches KV via a private endpoint (modules/privateendpoints).
#   bypass = "AzureServices" allows Azure platform services (Azure Monitor, etc.) through.
#

resource "random_string" "kv_suffix" {
  length  = 4
  upper   = false
  special = false
}

resource "azurerm_key_vault" "main" {
  #checkov:skip=CKV2_AZURE_32: Private endpoint for KV provisioned in modules/privateendpoints — not visible from this module
  # If key_vault_suffix override is set (recovery scenario), use it; else use the random one.
  name = "${var.name_prefix}-kv-${var.key_vault_suffix != "" ? var.key_vault_suffix : random_string.kv_suffix.result}"

  location                    = var.location
  resource_group_name         = var.resource_group_name
  enabled_for_disk_encryption = true
  tenant_id                   = var.tenant_id
  sku_name                    = "standard"
  tags                        = var.tags

  # CKV_AZURE_42: soft delete is enforced by AzureRM 3.x+ automatically.
  # 90 days: deleted secrets can be recovered for 90 days before permanent removal.
  soft_delete_retention_days = 90

  # CKV_AZURE_110: even admins cannot permanently delete the vault.
  # Prevents accidental (or malicious) destruction of credentials.
  purge_protection_enabled = true

  # Public network access must be true so the deployer can seed secrets from their
  # workstation during terraform apply. At runtime, AKS uses the private endpoint.
  # In a fully locked-down setup, this would be false (all access via private endpoint).
  public_network_access_enabled = true

  # Use Azure RBAC for fine-grained access control (instead of legacy access policies)
  rbac_authorization_enabled = true

  network_acls {
    default_action = "Deny"     # deny all unless explicitly allowed
    bypass         = "AzureServices"  # Azure Monitor, ARM, etc. can still reach KV

    # Allow deployer workstation or CI/CD runner IPs during terraform apply.
    # These are only needed when seeding secrets — runtime access goes via private endpoint.
    ip_rules = var.deployer_ips
  }
}

# Grant Key Vault Administrator to the deploying principal.
# This role allows creating/reading/updating secrets during the initial setup.
# In a production environment, this would be scoped to a break-glass account
# and removed after the initial seed. For this stack it stays for operational convenience.
resource "azurerm_role_assignment" "kv_admin" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = var.object_id  # whoever ran terraform apply
}
