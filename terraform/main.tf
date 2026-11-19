data "azurerm_client_config" "current" {}
data "azurerm_subscription" "current" {}

locals {
  name_prefix = "${var.project_name}-${var.environment}"
  common_tags = merge(var.tags, {
    Environment = var.environment
  })
}

resource "azurerm_resource_group" "main" {
  name     = "${local.name_prefix}-rg"
  location = var.location
  tags     = local.common_tags
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${local.name_prefix}-law"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.common_tags
}

module "networking" {
  source = "./modules/networking"

  name_prefix         = local.name_prefix
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

module "acr" {
  source = "./modules/acr"

  name_prefix         = local.name_prefix
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

module "keyvault" {
  source = "./modules/keyvault"

  name_prefix         = local.name_prefix
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = data.azurerm_client_config.current.object_id
  deployer_ips        = var.deployer_ips
  tags                = local.common_tags
}

module "aks" {
  source = "./modules/aks"

  name_prefix                = local.name_prefix
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  kubernetes_version         = var.kubernetes_version
  node_subnet_id             = module.networking.aks_subnet_id
  system_node_pool_vm_size   = var.system_node_pool_vm_size
  node_pool_vm_size          = var.node_pool_vm_size
  node_pool_min_count        = var.node_pool_min_count
  node_pool_max_count        = var.node_pool_max_count
  user_node_pool_min_count   = var.user_node_pool_min_count
  user_node_pool_max_count   = var.user_node_pool_max_count
  admin_group_object_ids     = [azuread_group.aks_admins.object_id]
  authorized_ip_ranges       = var.authorized_ip_ranges
  acr_id                     = module.acr.id
  key_vault_id               = module.keyvault.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.common_tags

  depends_on = [module.networking, module.acr]
}

# Private endpoints must come after AKS so the VNet already exists and
# the DNS zones resolve correctly when the kubelet first starts.
module "privateendpoints" {
  source = "./modules/privateendpoints"

  name_prefix         = local.name_prefix
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  vnet_id             = module.networking.vnet_id
  pe_subnet_id        = module.networking.pe_subnet_id
  key_vault_id        = module.keyvault.id
  tags                = local.common_tags

  depends_on = [module.networking, module.acr, module.keyvault]
}

# ── AKS Admin Group ───────────────────────────────────────────────────────────
# Creates the AAD group and adds the deploying user as the first member.
# Anyone added to this group later gets cluster-admin without a Terraform change.

resource "azuread_group" "aks_admins" {
  display_name     = "${local.name_prefix}-aks-admins"
  mail_enabled     = false
  security_enabled = true
}

resource "azuread_group_member" "aks_admin_deployer" {
  group_object_id  = azuread_group.aks_admins.object_id
  member_object_id = data.azurerm_client_config.current.object_id
}

# ── GitHub Actions OIDC Service Principal ─────────────────────────────────────
# No stored client secret — GitHub Actions authenticates via federated identity.

resource "azuread_application" "cicd" {
  display_name = "${local.name_prefix}-cicd"
}

resource "azuread_service_principal" "cicd" {
  client_id = azuread_application.cicd.client_id
}

resource "azuread_application_federated_identity_credential" "github_main" {
  application_id = azuread_application.cicd.id
  display_name   = "github-main"
  description    = "GitHub Actions pushes to main branch"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/main"
}

# Contributor on the subscription — Terraform needs to create/modify all resources.
resource "azurerm_role_assignment" "cicd_contributor" {
  scope                = data.azurerm_subscription.current.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.cicd.object_id
}

# AcrPush — CI pipeline pushes built images to ACR.
resource "azurerm_role_assignment" "cicd_acr_push" {
  scope                = module.acr.id
  role_definition_name = "AcrPush"
  principal_id         = azuread_service_principal.cicd.object_id
}
