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

variable "node_pool_vm_size" {
  description = "VM size for node pools. Standard_D4s_v3 (4 vCPU/16GB) is the minimum for LGTM stack + app pods"
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

variable "tags" {
  description = "Common resource tags"
  type        = map(string)
  default = {
    Project   = "peerisland"
    ManagedBy = "terraform"
  }
}
