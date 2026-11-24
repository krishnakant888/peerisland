# ── Terraform providers + remote state backend ────────────────────────────────
#
# PURPOSE: Declares all providers and configures the Azure Blob remote backend
#   where Terraform stores its state file.
#
# BOOTSTRAP ORDER:
#   1. Run terraform/bootstrap/ first — creates the storage account.
#   2. bootstrap.py phase 2 patches storage_account_name in this file automatically.
#   3. Run `terraform init -reconfigure` to migrate local → remote state.
#
# WHY use_azuread_auth = true?
#   Avoids storing a storage account key in environment variables or CI secrets.
#   The current AAD identity (developer, CI service principal) must have
#   "Storage Blob Data Owner" on the tfstate container — bootstrap.py phase 2
#   grants that role after creating the storage account.
#
# WHY ~> 4.0 for azurerm?
#   azurerm 4.x changed many defaults (subscription_id required, purge_soft_delete
#   disabled, etc.) that improve security posture.  Pinning to 4.x prevents an
#   accidental major-version upgrade mid-project.
#
# WHY helm provider reads ~/.kube/config (static path)?
#   If the helm provider depended on computed AKS outputs for its kubeconfig,
#   Terraform would fail on fresh runs with "Invalid provider configuration"
#   because provider blocks are evaluated before resources.  The static path
#   is populated by phase 5 of bootstrap.py (`az aks get-credentials`), and
#   the two-pass apply in phase 4 ensures AKS exists before helm runs.
#
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
