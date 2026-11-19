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
  default     = "Standard_D2s_v3"
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

variable "tags" {
  type    = map(string)
  default = {}
}
