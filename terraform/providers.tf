terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.90.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.47.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6.0"
    }
  }

  # Run terraform/bootstrap first to provision the storage account, then fill in
  # storage_account_name from its output and run: terraform init -reconfigure
  backend "azurerm" {
    resource_group_name  = "peerisland-tfstate-rg"
    storage_account_name = "REPLACE_WITH_BOOTSTRAP_OUTPUT"
    container_name       = "tfstate"
    key                  = "peerisland/prod.tfstate"
    use_azuread_auth     = true  # no storage key in environment; uses current AAD identity
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
}

provider "azuread" {}
provider "random" {}
