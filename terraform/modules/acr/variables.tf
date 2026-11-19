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
  description = "ACR SKU - Standard is sufficient for single-region deployments"
  type        = string
  default     = "Standard"
}

variable "tags" {
  type    = map(string)
  default = {}
}
