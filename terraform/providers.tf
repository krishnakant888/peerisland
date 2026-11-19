terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.16"
    }
  }

  # Run terraform/bootstrap first to provision the storage account, then fill in
  # storage_account_name from its output and run: terraform init -reconfigure
  backend "azurerm" {
    resource_group_name  = "peerisland-tfstate-rg"
    storage_account_name = "peerislandtfjncbl7fq"
    container_name       = "tfstate"
    key                  = "peerisland/prod.tfstate"
    use_azuread_auth     = true  # no storage key in environment; uses current AAD identity
  }
}

provider "azurerm" {
  # subscription_id sourced from ARM_SUBSCRIPTION_ID env var (set by bootstrap.py)
  subscription_id = var.subscription_id

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

# Helm provider — authenticates to AKS using kubelogin with the current
# Azure CLI session, same mechanism bootstrap.py and kubectl use.
provider "helm" {
  kubernetes {
    host = module.aks.host
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "kubelogin"
      args = [
        "get-token",
        "--login",    "azurecli",
        "--server-id", "6dae42f8-4368-4678-94ff-3960e28e3630",
      ]
    }
  }
}
