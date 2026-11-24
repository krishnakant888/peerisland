# ── ACR module — Azure Container Registry ─────────────────────────────────────
#
# PURPOSE: Creates the private container registry that stores backend and frontend
#   Docker images.  GitHub Actions builds and pushes images here; AKS pulls from
#   here using a managed identity (AcrPull role, assigned in modules/aks/main.tf).
#
# WHY admin_enabled = false?
#   Admin credentials are static username/password — a security anti-pattern.
#   AKS uses managed identity instead: no credentials to rotate or leak.
#   The kubelet's managed identity is granted "AcrPull" in the AKS module.
#
# WHY Standard SKU?
#   Standard supports private endpoints, managed identities, and all features
#   needed for this stack.  Premium adds geo-replication and quarantine, which
#   require multiple Azure regions — overkill for a single-region deployment.
#   The checkov:skip annotations document exactly which Premium-only checks are
#   intentionally bypassed.
#
# NAME: replace("${name_prefix}acr", "-", "") — ACR names must be alphanumeric
#   only (no hyphens).  So "peerisland-prod" becomes "peerislandprodacr".
#
resource "azurerm_container_registry" "main" {
  #checkov:skip=CKV_AZURE_166: Quarantine requires Premium SKU — Standard used to minimise cost
  #checkov:skip=CKV_AZURE_164: Content Trust (Notary v1) deprecated by Azure, removed March 2028
  #checkov:skip=CKV_AZURE_139: Public network access required — no private endpoint for ACR in this design
  #checkov:skip=CKV_AZURE_237: Dedicated data endpoints require Premium SKU
  #checkov:skip=CKV_AZURE_233: Zone redundancy requires Premium SKU
  #checkov:skip=CKV_AZURE_165: Geo-replication is Premium-only; single-region deployment
  #checkov:skip=CKV_AZURE_167: Untagged manifest retention policy requires Premium SKU
  name                = replace("${var.name_prefix}acr", "-", "")
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.sku
  tags                = var.tags

  # Admin access disabled; AKS uses managed identity pull
  admin_enabled = false
}
