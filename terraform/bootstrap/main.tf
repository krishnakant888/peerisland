# Run this ONCE before the main terraform config.
# It creates the storage account that will hold the main Terraform state file.
#
# Usage:
#   cd terraform/bootstrap
#   terraform init
#   terraform apply
#
# After apply, copy the storage_account_name output into providers.tf:
#   backend "azurerm" {
#     resource_group_name  = "peerisland-tfstate-rg"
#     storage_account_name = "<output value>"
#     container_name       = "tfstate"
#     key                  = "peerisland/prod.tfstate"
#     use_azuread_auth     = true   # no connection string in environment
#   }
#
# Then run: cd ../  &&  terraform init -reconfigure

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.90.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6.0"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "environment" {
  type    = string
  default = "prod"
}

resource "azurerm_resource_group" "tfstate" {
  name     = "peerisland-tfstate-rg"
  location = var.location
}

# Random suffix prevents name collisions — storage account names must be globally unique
resource "random_string" "suffix" {
  length  = 8
  lower   = true
  numeric = true
  special = false
  upper   = false
}

resource "azurerm_storage_account" "tfstate" {
  name                     = "peerislandtf${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.tfstate.name
  location                 = azurerm_resource_group.tfstate.location
  account_tier             = "Standard"
  account_replication_type = "GRS"  # geo-redundant — state file is critical

  # Terraform state must never be lost or corrupted
  blob_properties {
    versioning_enabled  = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  min_tls_version           = "TLS1_2"
  enable_https_traffic_only = true

  tags = {
    Project   = "peerisland"
    ManagedBy = "terraform-bootstrap"
    Purpose   = "terraform-state"
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_name  = azurerm_storage_account.tfstate.name
  container_access_type = "private"
}

# Grant the current principal Storage Blob Data Owner so Terraform can read/write state
# without a storage account key (use_azuread_auth = true in backend block)
data "azurerm_client_config" "current" {}

resource "azurerm_role_assignment" "tfstate_owner" {
  scope                = azurerm_storage_account.tfstate.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id         = data.azurerm_client_config.current.object_id
}

output "storage_account_name" {
  value       = azurerm_storage_account.tfstate.name
  description = "Paste this into the backend block in providers.tf"
}

output "resource_group_name" {
  value = azurerm_resource_group.tfstate.name
}
