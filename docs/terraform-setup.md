# Terraform Setup & Deployment

## Prerequisites

| Tool | Version |
|------|---------|
| Terraform | >= 1.5.0 |
| Azure CLI | >= 2.55 |
| az account | Logged in with Contributor + UAA on target subscription |

## Architecture

```
Resource Group: peerisland-prod-rg
├── Virtual Network (10.0.0.0/8)
│   ├── aks-subnet     (10.240.0.0/16)  ← node pool + pods
│   └── appgw-subnet   (10.241.0.0/24)  ← future App Gateway
├── AKS Cluster (peerisland-prod-aks)
│   ├── System node pool  (2–5 × Standard_DS2_v2)
│   └── User node pool    (1–5 × Standard_DS2_v2)
├── Azure Container Registry (peerislandprodacr)
├── Azure Key Vault (peerisland-prod-kv)
└── Log Analytics Workspace (peerisland-prod-law)
```

## First-Time Setup

### 1. Bootstrap Terraform state storage

```bash
# Create a resource group and storage account for the Terraform state
az group create -n tfstate-rg -l eastus
az storage account create -n tfstateXXXXXX -g tfstate-rg -l eastus --sku Standard_LRS
az storage container create -n tfstate --account-name tfstateXXXXXX
```

Uncomment the `backend "azurerm"` block in `terraform/providers.tf` and set the storage account name.

### 2. Create a Service Principal for CI/CD

```bash
az ad sp create-for-rbac \
  --name "peerisland-cicd" \
  --role Contributor \
  --scopes /subscriptions/<SUB_ID> \
  --sdk-auth
```

Store the JSON output as a GitHub secret named `AZURE_CREDENTIALS`, then also set:
- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

### 3. Deploy infrastructure

```bash
cd terraform

# Copy and edit variable values
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — set admin_group_object_ids and authorized_ip_ranges

terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

Typical apply time: **12–18 minutes** (AKS provisioning dominates).

### 4. Configure kubectl

```bash
# Output from terraform apply:
az aks get-credentials \
  --resource-group peerisland-prod-rg \
  --name peerisland-prod-aks \
  --overwrite-existing

kubectl get nodes
```

## Security Controls

| Control | Implementation |
|---------|---------------|
| RBAC | `role_based_access_control_enabled = true`, `azure_rbac_enabled = true` |
| No local accounts | `local_account_disabled = true` — forces Azure AD auth |
| API server IP restriction | `api_server_authorized_ip_ranges` |
| Network policy | Azure CNI + Azure network policy (east-west deny-by-default) |
| Node encryption | `enable_host_encryption = true` |
| ACR admin disabled | `admin_enabled = false` — kubelet uses managed identity |
| Key Vault RBAC | `enable_rbac_authorization = true`, public access off |
| Auto-upgrade | `automatic_channel_upgrade = "stable"` |
| Defender for Containers | `microsoft_defender` block |

## Destroying the environment

```bash
terraform destroy
```

> **Warning**: this deletes the AKS cluster and all persistent data. The Key Vault has purge protection; it will be soft-deleted for 90 days.
