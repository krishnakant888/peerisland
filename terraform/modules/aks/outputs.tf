# ── AKS module outputs ────────────────────────────────────────────────────────
#
# cluster_name / cluster_id: surfaced to root outputs; CI/CD writes cluster_name
#   to GitHub Actions secret AKS_CLUSTER_NAME.
#
# kubelet_identity_object_id: the managed identity that runs on every node and
#   is used for AcrPull.  Root module's AcrPull role assignment targets this.
#
# kube_config (sensitive): the raw kubeconfig — not used directly; bootstrap.py
#   writes it via `az aks get-credentials` to avoid storing it in state outputs.
#
# node_resource_group: the MC_* group AKS creates for node VMs, NSGs, and LBs.
#   The AKS module's NSG rules attach to the NSG inside this group.
#
# kv_secrets_provider_client_id: the add-on managed identity that the CSI
#   secrets driver uses to authenticate to Key Vault.  It is wired into the
#   ServiceAccount annotation and SecretProviderClass (k8s/serviceaccount.yaml
#   and k8s/monitoring/secret-provider-grafana.yaml).
#
output "cluster_name" {
  value = azurerm_kubernetes_cluster.main.name
}

output "cluster_id" {
  value = azurerm_kubernetes_cluster.main.id
}

output "kubelet_identity_object_id" {
  value = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
}

output "kube_config" {
  value     = azurerm_kubernetes_cluster.main.kube_config_raw
  sensitive = true
}

output "host" {
  value     = azurerm_kubernetes_cluster.main.kube_config[0].host
  sensitive = true
}

output "cluster_ca_certificate" {
  value     = azurerm_kubernetes_cluster.main.kube_config[0].cluster_ca_certificate
  sensitive = true
}

output "node_resource_group" {
  value       = azurerm_kubernetes_cluster.main.node_resource_group
  description = "The MC_* resource group that AKS manages (node VMs, NSGs, LBs)."
}

output "kv_secrets_provider_client_id" {
  value       = data.azurerm_user_assigned_identity.kv_secrets_provider.client_id
  description = "Client ID of the KV secrets provider managed identity — used in SecretProviderClass and SA annotations."
}
