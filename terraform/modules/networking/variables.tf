# ── Networking module inputs ──────────────────────────────────────────────────
#
# PURPOSE: Declares the address spaces for the VNet and its three subnets.
#
# SUBNET DESIGN:
#   10.0.0.0/8  — VNet supernet (huge range for future growth)
#   10.240.0.0/16 — aks-subnet: AKS pods + nodes live here (/16 = 65k IPs;
#     Azure CNI assigns a real VNet IP to every pod so you need enough room)
#   10.241.0.0/24 — appgw-subnet: reserved for Application Gateway (optional);
#     separated because AppGW requires its own dedicated subnet
#   10.242.0.0/24 — pe-subnet: private endpoint NICs for ACR and Key Vault;
#     private_endpoint_network_policies must be disabled on this subnet
#
# WHY separate subnets?
#   Network Security Groups and routing policies apply at the subnet level.
#   Separating AKS nodes from private endpoints and AppGW prevents their
#   traffic rules from interfering with each other.
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

variable "vnet_address_space" {
  type    = list(string)
  default = ["10.0.0.0/8"]
}

variable "aks_subnet_cidr" {
  type    = string
  default = "10.240.0.0/16"
}

variable "appgw_subnet_cidr" {
  type    = string
  default = "10.241.0.0/24"
}

variable "pe_subnet_cidr" {
  description = "CIDR for the private endpoint subnet"
  type        = string
  default     = "10.242.0.0/24"
}

variable "tags" {
  type    = map(string)
  default = {}
}
