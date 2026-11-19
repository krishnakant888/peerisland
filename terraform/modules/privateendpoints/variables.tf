variable "name_prefix" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "vnet_id" {
  description = "VNet to link the private DNS zones to"
  type        = string
}

variable "pe_subnet_id" {
  description = "Subnet for private endpoint NICs (must have private_endpoint_network_policies = Disabled)"
  type        = string
}

variable "acr_id" {
  description = "Resource ID of the Container Registry"
  type        = string
}

variable "key_vault_id" {
  description = "Resource ID of the Key Vault"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
