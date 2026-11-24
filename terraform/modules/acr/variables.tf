# ── ACR module inputs ─────────────────────────────────────────────────────────
# name_prefix    → "peerisland-prod"  (hyphens removed before use in resource name)
# resource_group_name / location / tags → passed from root module's common locals
# sku            → Standard (default); override to Premium for geo-replication
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

variable "sku" {
  description = "ACR SKU - Standard is sufficient for single-region deployments"
  type        = string
  default     = "Standard"
}

variable "tags" {
  type    = map(string)
  default = {}
}
