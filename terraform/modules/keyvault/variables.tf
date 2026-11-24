# ── Key Vault module inputs ───────────────────────────────────────────────────
#
# PURPOSE: Configures a Key Vault with RBAC authorization, purge protection, and
#   network ACLs.  Stores Grafana admin credentials; AKS reads them via the CSI
#   secrets provider add-on without any static credentials in k8s Secrets.
#
# key_vault_suffix EXPLAINED:
#   KV names must be globally unique across Azure.  Terraform uses a random_string
#   suffix by default.  After `terraform destroy`, the KV enters soft-delete
#   (name reserved for 90 days).  Set key_vault_suffix in tfvars to match the
#   existing soft-deleted KV name so Terraform recovers it rather than failing.
#
# object_id: the deployer's AAD object ID — granted "Key Vault Administrator"
#   during apply so bootstrap.py can write Grafana credentials.  After bootstrap
#   this role can be removed if following least-privilege principles.
#
# deployer_ips: KV network ACL.  Only these source IPs can reach the data plane
#   during terraform apply (to write secrets).  After deployment, only the
#   private endpoint serves traffic from within the VNet.
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

variable "key_vault_suffix" {
  description = "Override KV name suffix (set in tfvars after state loss to match existing KV). Empty = use random."
  type        = string
  default     = ""
}

variable "tenant_id" {
  type = string
}

variable "object_id" {
  description = "Object ID of the deploying principal (for initial admin access)"
  type        = string
}

variable "deployer_ips" {
  description = "Public IPs allowed through the Key Vault network ACL (CI/CD runner, operator machine)"
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
