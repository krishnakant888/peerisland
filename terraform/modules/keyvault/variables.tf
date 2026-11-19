variable "name_prefix" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
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
