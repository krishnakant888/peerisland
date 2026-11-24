# ── Root Terraform configuration ──────────────────────────────────────────────
#
# MODULE STRUCTURE — why modules instead of one flat file?
#   Each module encapsulates a logical infrastructure unit. This makes it
#   possible to plan/apply changes to one piece (e.g. AKS) without touching
#   others (e.g. networking), and to reuse modules across environments.
#
#   networking    → VNet, subnets (aks, appgw, pe), NSG, subnet associations
#   acr           → Azure Container Registry (private endpoint handled separately)
#   keyvault      → Key Vault, RBAC, network ACLs, soft-delete
#   aks           → AKS cluster, system + user node pools, OIDC, workload identity
#   privateendpoints → Private DNS zones, private endpoints for ACR and Key Vault
#
# DEPENDENCY ORDER:
#   networking → aks (needs subnet IDs)
#   networking + acr + keyvault → privateendpoints (needs resource IDs)
#   aks depends_on networking + acr (node pool needs VNet + ACR pull permissions)
#
# WHAT IS NOT IN TERRAFORM:
#   ArgoCD, cert-manager, Argo Rollouts, monitoring stack (Alloy, Mimir, Loki, Tempo)
#   are all installed by bootstrap.py using Helm. They are intentionally NOT
#   terraform resources to avoid import conflicts and Helm provider fights over
#   ArgoCD's configmap. Bootstrap owns the initial cluster configuration;
#   ArgoCD owns the app layer from that point on.
#

locals {
  # sub is used in import blocks below — keep it short
  sub         = var.subscription_id
  # Resource group name: peerisland-prod-rg
  rg          = "${var.project_name}-${var.environment}-rg"
  # Prefix used in all resource names: peerisland-prod
  name_prefix = "${var.project_name}-${var.environment}"
  # Merge caller-supplied tags with environment tag
  common_tags = merge(var.tags, { Environment = var.environment })
}

# Who is running terraform apply right now? Used for RBAC role assignments.
data "azurerm_client_config" "current" {}
# Subscription metadata — needed for role assignment scopes
data "azurerm_subscription" "current" {}

# ── Resource Group ─────────────────────────────────────────────────────────────
resource "azurerm_resource_group" "main" {
  name     = "${local.name_prefix}-rg"
  location = var.location
  tags     = local.common_tags
}

# ── Log Analytics Workspace ────────────────────────────────────────────────────
# AKS OMS agent streams container logs and metrics here.
# Also used for Azure Monitor alerts and diagnostic settings.
resource "azurerm_log_analytics_workspace" "main" {
  name                = "${local.name_prefix}-law"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"  # pay-per-GB, most cost-effective for small scale
  retention_in_days   = 30
  tags                = local.common_tags
}

# ── Networking module ──────────────────────────────────────────────────────────
# Creates: VNet, aks-subnet, appgw-subnet, pe-subnet, NSG, subnet-NSG association
module "networking" {
  source = "./modules/networking"

  name_prefix         = local.name_prefix
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

# ── ACR module ────────────────────────────────────────────────────────────────
# Creates: Azure Container Registry (Premium SKU for private endpoint support)
module "acr" {
  source = "./modules/acr"

  name_prefix         = local.name_prefix
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

# ── Key Vault module ──────────────────────────────────────────────────────────
# Creates: Key Vault with RBAC auth, purge protection, network ACLs
# Grafana admin credentials are stored here (see bottom of this file)
module "keyvault" {
  source = "./modules/keyvault"

  name_prefix         = local.name_prefix
  key_vault_suffix    = var.key_vault_suffix   # pin suffix after state loss
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = data.azurerm_client_config.current.object_id  # deployer gets KV Admin
  deployer_ips        = var.deployer_ips       # allow deployer workstation to reach KV
  tags                = local.common_tags
}

# ── AKS module ────────────────────────────────────────────────────────────────
# Creates: AKS cluster, system pool (kube-system), user pool (workloads),
#          OIDC issuer, workload identity, CSI secrets provider,
#          RBAC role assignments (AcrPull, KV Secrets User, Network Contributor),
#          NSG rules for HTTP/HTTPS on the AKS node resource group
module "aks" {
  source = "./modules/aks"

  name_prefix                = local.name_prefix
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  kubernetes_version         = var.kubernetes_version
  node_subnet_id             = module.networking.aks_subnet_id   # AKS pods live here
  system_node_pool_vm_size   = var.system_node_pool_vm_size
  node_pool_vm_size          = var.node_pool_vm_size
  node_pool_min_count        = var.node_pool_min_count
  node_pool_max_count        = var.node_pool_max_count
  user_node_pool_min_count   = var.user_node_pool_min_count
  user_node_pool_max_count   = var.user_node_pool_max_count
  admin_group_object_ids     = [azuread_group.aks_admins.object_id]
  authorized_ip_ranges       = var.authorized_ip_ranges   # restrict API server access
  acr_id                     = module.acr.id              # for AcrPull role
  key_vault_id               = module.keyvault.id         # for KV Secrets User role
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  vnet_id                    = module.networking.vnet_id  # for Network Contributor role
  tags                       = local.common_tags

  depends_on = [module.networking, module.acr]
}

# ── Private Endpoints module ──────────────────────────────────────────────────
# Creates: Private endpoints + DNS zones for ACR and Key Vault.
# Must come AFTER AKS so the VNet exists and DNS resolves correctly when
# the kubelet starts and tries to pull images from ACR via the private endpoint.
module "privateendpoints" {
  source = "./modules/privateendpoints"

  name_prefix         = local.name_prefix
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  vnet_id             = module.networking.vnet_id
  pe_subnet_id        = module.networking.pe_subnet_id  # dedicated subnet for PE NICs
  key_vault_id        = module.keyvault.id
  tags                = local.common_tags

  depends_on = [module.networking, module.acr, module.keyvault]
}

# ArgoCD is installed by bootstrap.py (Helm), not Terraform.
# This avoids import conflicts and Helm provider fights over argocd-cm config.

# ── AKS Admin Group ───────────────────────────────────────────────────────────
# Anyone added to this AAD group gets cluster-admin via Azure RBAC.
# No kubeconfig files needed — az aks get-credentials + group membership = access.
resource "azuread_group" "aks_admins" {
  display_name     = "${local.name_prefix}-aks-admins"
  mail_enabled     = false
  security_enabled = true
}

# Add the person running terraform apply as the first group member (bootstrap admin)
resource "azuread_group_member" "aks_admin_deployer" {
  group_object_id  = azuread_group.aks_admins.object_id
  member_object_id = data.azurerm_client_config.current.object_id
}

# ── GitHub Actions OIDC Service Principal ─────────────────────────────────────
# No stored client secret — GitHub Actions authenticates via OIDC federated identity.
# Flow: GitHub generates a JWT → Azure validates it → Azure issues access token.
# The credential below restricts token exchange to commits on the main branch only.
resource "azuread_application" "cicd" {
  display_name = "${local.name_prefix}-cicd"
}

resource "azuread_service_principal" "cicd" {
  client_id = azuread_application.cicd.client_id
}

# Import block: this federated credential was created before Terraform managed it.
# import blocks make terraform adopt an existing Azure resource without recreating it.
# Remove this block once the resource is in state.
import {
  to = azuread_application_federated_identity_credential.github_main
  id = "d3389612-559a-4f7f-a903-a1af271ee122/federatedIdentityCredential/4f439d04-ae63-43bf-ac5f-008b7459288d"
}

resource "azuread_application_federated_identity_credential" "github_main" {
  application_id = azuread_application.cicd.id
  display_name   = "github-main"
  description    = "GitHub Actions pushes to main branch"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  # Restricts this credential to tokens from the main branch only.
  # A PR branch or fork cannot exchange a token using this credential.
  subject        = "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/main"
}

# CI/CD service principal needs Contributor on the subscription to create resources
resource "azurerm_role_assignment" "cicd_contributor" {
  scope                = data.azurerm_subscription.current.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.cicd.object_id
}

# CI/CD also needs AcrPush to push built images to ACR
resource "azurerm_role_assignment" "cicd_acr_push" {
  scope                = module.acr.id
  role_definition_name = "AcrPush"
  principal_id         = azuread_service_principal.cicd.object_id
}

# ── Grafana credentials in Key Vault ─────────────────────────────────────────
# Flow: terraform.tfvars → Key Vault → CSI driver → k8s Secret → Grafana Helm chart
#
# ignore_changes = [value]: once the secret is seeded, Terraform won't
# overwrite it on subsequent applies. This allows credential rotation:
#   az keyvault secret set --name grafana-admin-password --vault-name <kv> --value <new>
# without touching terraform.tfvars.
resource "azurerm_key_vault_secret" "grafana_admin_user" {
  name         = "grafana-admin-user"
  value        = var.grafana_admin_user
  content_type = "text/plain"
  key_vault_id = module.keyvault.id

  # checkov:skip=CKV_AZURE_41: Credential rotated via tfvars, not by expiry policy
  depends_on = [module.keyvault]

  lifecycle {
    ignore_changes = [value]  # allow external rotation without Terraform drift
  }
}

resource "azurerm_key_vault_secret" "grafana_admin_password" {
  name         = "grafana-admin-password"
  value        = var.grafana_admin_password
  content_type = "text/plain"
  key_vault_id = module.keyvault.id

  # checkov:skip=CKV_AZURE_41: Credential rotated via tfvars, not by expiry policy
  depends_on = [module.keyvault]

  lifecycle {
    ignore_changes = [value]
  }
}
