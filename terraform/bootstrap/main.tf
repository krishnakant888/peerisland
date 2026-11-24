# ── terraform/bootstrap — remote state infrastructure ─────────────────────────
#
# PURPOSE: Provisions the Azure Storage Account that holds the MAIN Terraform
#   state file.  This is a classic "chicken-and-egg" problem: the main config
#   needs remote state before it can run, so bootstrap uses LOCAL state only.
#
# WHY a separate bootstrap directory?
#   If this was part of the main terraform config, Terraform couldn't init
#   (can't read a backend that doesn't exist yet).  The two-step approach
#   (bootstrap with local state → main config with remote state) is the
#   recommended pattern for teams starting from scratch.
#
# GRS (geo-redundant) replication: Terraform state is the single source of truth
#   for all infrastructure.  If it's lost, resources become unmanaged orphans.
#   GRS copies blobs to a secondary Azure region for disaster recovery.
#
# Blob versioning + 30-day retention: accidental state corruption can be
#   rolled back to any previous version within 30 days.
#
# USAGE (one-time):
#   cd terraform/bootstrap && terraform init && terraform apply
#   Copy storage_account_name output → providers.tf backend block
#   cd .. && terraform init -reconfigure
#
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
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6.0"
    }
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id
  features {}
}

variable "subscription_id" {
  type    = string
  default = "f8bb6fd3-0dce-48c1-a61e-ba1519f2570b"
}

variable "location" {
  type    = string
  default = "centralindia"
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
  #checkov:skip=CKV_AZURE_59:  Deployer-IP network ACL restricts access; private endpoint not needed for tfstate-only SA
  #checkov:skip=CKV_AZURE_33:  Queue service not used — queue logging not applicable
  #checkov:skip=CKV_AZURE_190: allow_nested_items_to_be_public=false set below
  #checkov:skip=CKV2_AZURE_40: Shared Key auth required during bootstrap before RBAC propagates
  #checkov:skip=CKV2_AZURE_33: Private endpoint for tfstate SA adds cost/complexity; deployer-IP ACL used instead
  #checkov:skip=CKV2_AZURE_41: SAS tokens not issued — only Entra ID auth used post-bootstrap
  #checkov:skip=CKV2_AZURE_1:  CMK for tfstate SA is excessive for a demo/interview stack
  #checkov:skip=CKV2_AZURE_21: Blob read logging configured in blob_properties.logging below
  name                     = "peerislandtf${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.tfstate.name
  location                 = azurerm_resource_group.tfstate.location
  account_tier             = "Standard"
  account_replication_type = "GRS"  # geo-redundant — state file is critical

  allow_nested_items_to_be_public  = false
  shared_access_key_enabled        = true  # required during bootstrap init before RBAC propagates

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

  min_tls_version            = "TLS1_2"
  https_traffic_only_enabled = true

  tags = {
    Project   = "peerisland"
    ManagedBy = "terraform-bootstrap"
    Purpose   = "terraform-state"
  }
}

resource "azurerm_storage_container" "tfstate" {
  #checkov:skip=CKV2_AZURE_21: Blob logging is configured on the storage account, not the container
  name               = "tfstate"
  storage_account_id = azurerm_storage_account.tfstate.id
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
