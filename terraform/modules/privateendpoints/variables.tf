# ── Private Endpoints module inputs ──────────────────────────────────────────
# vnet_id      → used to link the private DNS zone to the VNet so cluster DNS
#                resolves KV hostnames to private IPs
# pe_subnet_id → subnet where the PE NIC will be placed (private_endpoint_network_policies
#                must be disabled on this subnet — set in networking module)
# key_vault_id → the KV resource to connect to; determines which private DNS
#                zone records are created (privatelink.vaultcore.azure.net)
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

variable "vnet_id" {
  description = "VNet to link the private DNS zones to"
  type        = string
}

variable "pe_subnet_id" {
  description = "Subnet for private endpoint NICs (must have private_endpoint_network_policies = Disabled)"
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
