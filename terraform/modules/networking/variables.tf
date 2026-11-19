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
