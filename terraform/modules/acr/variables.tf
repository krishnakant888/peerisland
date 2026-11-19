variable "name_prefix" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "sku" {
  description = "ACR SKU - Premium required for geo-replication and content trust"
  type        = string
  default     = "Premium"
}

variable "tags" {
  type    = map(string)
  default = {}
}
