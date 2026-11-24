# ── AKS module inputs ─────────────────────────────────────────────────────────
#
# PURPOSE: Declares all configuration for the AKS cluster, its two node pools,
#   OIDC/workload identity, RBAC, and monitoring integration.
#
# TWO NODE POOLS explained:
#   system pool (node_pool_*): runs kube-system DaemonSets + control-plane
#     adjacent components.  Small SKU (D2s_v3) is fine — it never runs app pods.
#     Tainted with CriticalAddonsOnly so only system workloads land here.
#   user pool (user_node_pool_*): runs all application pods, monitoring stack
#     (Alloy, Mimir, Loki, Tempo, Grafana), and ArgoCD.  Standard_D4s_v5
#     (4 vCPU / 16 GB) is the minimum that fits the full LGTM observability stack.
#
# authorized_ip_ranges: Restricts who can reach the Kubernetes API server
#   (kubectl, terraform, ArgoCD, CI runners).  Empty = public by default.
#   Production should always set this to CI runner IPs + operator workstations.
#
# vnet_id: AKS needs "Network Contributor" on the VNet so it can provision
#   Azure Load Balancer rules when Services of type LoadBalancer are created.
#
variable "name_prefix" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "kubernetes_version" {
  type = string
}

variable "node_subnet_id" {
  type = string
}

variable "system_node_pool_vm_size" {
  description = "VM size for the system node pool — only runs kube-system DaemonSets and control-plane-adjacent pods. A smaller SKU is fine."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "node_pool_vm_size" {
  description = "VM size for user node pools — runs all application workloads."
  type        = string
  default     = "Standard_DS2_v2"
}

variable "node_pool_min_count" {
  type    = number
  default = 2
}

variable "user_node_pool_min_count" {
  type    = number
  default = 2
}

variable "user_node_pool_max_count" {
  type    = number
  default = 5
}

variable "node_pool_max_count" {
  type    = number
  default = 5
}

variable "admin_group_object_ids" {
  type    = list(string)
  default = []
}

variable "authorized_ip_ranges" {
  description = "IP ranges allowed to reach the Kubernetes API server"
  type        = list(string)
  default     = []
}

variable "acr_id" {
  type = string
}

variable "key_vault_id" {
  type = string
}

variable "log_analytics_workspace_id" {
  type = string
}

variable "vnet_id" {
  description = "ID of the VNet where the AKS cluster is deployed; used to grant the cluster identity Network Contributor for LoadBalancer provisioning."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
