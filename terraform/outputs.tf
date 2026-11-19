output "resource_group_name" {
  description = "Name of the main resource group"
  value       = azurerm_resource_group.main.name
}

output "aks_cluster_name" {
  description = "Name of the AKS cluster"
  value       = module.aks.cluster_name
}

output "aks_cluster_id" {
  description = "Resource ID of the AKS cluster"
  value       = module.aks.cluster_id
}

output "acr_login_server" {
  description = "ACR login server URL"
  value       = module.acr.login_server
}

output "acr_name" {
  description = "ACR resource name"
  value       = module.acr.name
}

output "key_vault_uri" {
  description = "Key Vault URI"
  value       = module.keyvault.vault_uri
}

output "vnet_id" {
  description = "Virtual Network resource ID"
  value       = module.networking.vnet_id
}

output "log_analytics_workspace_id" {
  description = "Log Analytics Workspace ID"
  value       = azurerm_log_analytics_workspace.main.id
}

output "kubectl_configure_command" {
  description = "Command to configure local kubectl access"
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.main.name} --name ${module.aks.cluster_name} --overwrite-existing"
}

output "cicd_client_id" {
  description = "App ID of the GitHub Actions service principal — set as AZURE_CLIENT_ID in GitHub secrets"
  value       = azuread_application.cicd.client_id
}

output "cicd_tenant_id" {
  description = "Azure AD tenant ID — set as AZURE_TENANT_ID in GitHub secrets"
  value       = data.azurerm_client_config.current.tenant_id
}

output "cicd_subscription_id" {
  description = "Subscription ID — set as AZURE_SUBSCRIPTION_ID in GitHub secrets"
  value       = data.azurerm_subscription.current.subscription_id
}
