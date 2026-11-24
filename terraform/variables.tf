# ── Terraform root variables ──────────────────────────────────────────────────
#
# PURPOSE: Declares every input variable accepted by the root Terraform module.
#   All values come from terraform.tfvars (gitignored — contains credentials).
#
# WHY NOT hardcode values?
#   Variables let the same Terraform code deploy to different subscriptions,
#   regions, or environments by changing only terraform.tfvars.  This is the
#   standard Terraform multi-environment pattern.
#
# SENSITIVE VARIABLES: grafana_admin_password is marked sensitive=true which
#   prevents Terraform from printing its value in plan/apply output or logs.
#   It is also stored in Key Vault via azurerm_key_vault_secret in main.tf.
#
# key_vault_suffix: After an accidental `terraform destroy`, Key Vault enters
#   a 90-day soft-delete period with its name reserved.  The suffix lets you
#   pin the KV name to match the existing (soft-deleted) vault so Terraform
#   can recover it rather than fail with "name already taken".
#
# deployer_ips: The KV network ACL allows only these IPs.  Set this to your
#   CI/CD runner's egress IP + your workstation IP during `terraform apply`.
#
variable "subscription_id" {
  description = "Azure subscription ID (required by azurerm 4.x)"
  type        = string
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "prod"
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "centralindia"
}

variable "project_name" {
  description = "Project name used in all resource names"
  type        = string
  default     = "peerisland"
}

variable "kubernetes_version" {
  description = "Kubernetes version for AKS"
  type        = string
  default     = "1.37.1"
}

variable "system_node_pool_vm_size" {
  description = "VM size for the system node pool — only kube-system DaemonSets run here, so a 2-vCPU SKU is sufficient"
  type        = string
  default     = "Standard_D2s_v3"
}

variable "node_pool_vm_size" {
  description = "VM size for user node pools — runs all application workloads. Standard_D4s_v5 (4 vCPU/16GB) is the minimum for LGTM stack + app pods"
  type        = string
  default     = "Standard_D4s_v5"
}

variable "deployer_ips" {
  description = "Public IPs of the CI/CD pipeline or operator workstation that need to reach Key Vault during terraform apply"
  type        = list(string)
  default     = []
}

variable "node_pool_min_count" {
  description = "Minimum number of nodes for the system node pool"
  type        = number
  default     = 2
}

variable "user_node_pool_min_count" {
  description = "Minimum number of nodes for the user node pool"
  type        = number
  default     = 2
}

variable "user_node_pool_max_count" {
  description = "Maximum number of nodes for the user node pool"
  type        = number
  default     = 5
}

variable "node_pool_max_count" {
  description = "Maximum number of nodes"
  type        = number
  default     = 5
}

variable "authorized_ip_ranges" {
  description = "Authorized IP ranges for AKS API server access"
  type        = list(string)
  default     = []
}

variable "github_org" {
  description = "GitHub organisation or username that owns the repo (used in OIDC federated credential subject)"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (used in OIDC federated credential subject)"
  type        = string
  default     = "peerisland"
}

variable "key_vault_suffix" {
  description = "Pin KV name suffix after state loss (e.g. 'sqxc'). Empty = let terraform generate one."
  type        = string
  default     = ""
}

variable "grafana_admin_user" {
  description = "Grafana admin username"
  type        = string
  default     = "admin"
}

variable "grafana_admin_password" {
  description = "Grafana admin password — stored in Key Vault, never in state"
  type        = string
  sensitive   = true
  default     = ""
}

variable "tags" {
  description = "Common resource tags"
  type        = map(string)
  default = {
    Project   = "peerisland"
    ManagedBy = "terraform"
  }
}
