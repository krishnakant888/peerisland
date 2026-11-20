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
    storage_account_name = "peerislandtf4ej9kfkv"
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

# Helm provider — reads kubeconfig written by `az aks get-credentials`.
# Uses a static file path so provider init never depends on computed AKS
# outputs, which would cause "Invalid provider configuration" on fresh runs.
provider "helm" {
  kubernetes {
    config_path    = pathexpand("~/.kube/config")
    config_context = "${var.project_name}-${var.environment}-aks"
  }
}
