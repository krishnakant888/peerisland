# CI/CD Pipeline Workflow

## Overview

Three GitHub Actions workflows handle different concerns:

```
┌─────────────────────────────────────────────────────────────┐
│  PR opened / push to main (terraform/**)                    │
│  ┌────────────────────────────────────────────┐             │
│  │  terraform-scan.yml                         │             │
│  │  1. Checkov scan  → SARIF → Security tab    │             │
│  │  2. Terrascan scan → SARIF                  │             │
│  └────────────────────────────────────────────┘             │
│                                                             │
│  Push to main (app/**)                                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  build-push.yml                                      │    │
│  │  1. Azure OIDC login (no long-lived credentials)     │    │
│  │  2. docker buildx build (layer cache via GHA)        │    │
│  │  3. Push :sha + :latest to ACR                       │    │
│  │  4. Trivy scan — block on CRITICAL CVEs              │    │
│  │  5. Trigger deploy.yml                               │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  workflow_dispatch / triggered by build-push                │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  deploy.yml                                          │    │
│  │  1. Azure OIDC login                                 │    │
│  │  2. az aks get-credentials                          │    │
│  │  3. sed image tags into k8s YAML                    │    │
│  │  4. kubectl apply (namespace → manifests)           │    │
│  │  5. kubectl rollout status (5 min timeout)          │    │
│  │  6. Smoke-test /health endpoint                     │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## Required GitHub Secrets

| Secret | Value |
|--------|-------|
| `AZURE_CLIENT_ID` | Service principal client ID |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID |
| `ACR_NAME` | ACR resource name (e.g. `peerislandprodacr`) |
| `ACR_LOGIN_SERVER` | ACR login server (e.g. `peerislandprodacr.azurecr.io`) |
| `AKS_CLUSTER_NAME` | AKS cluster name |
| `AKS_RESOURCE_GROUP` | Resource group name |

> Authentication uses **Azure Federated Identity (OIDC)** — no stored client secrets. Configure the federated credential on the service principal to trust the GitHub Actions token for `refs/heads/main`.

## Setting up OIDC

```bash
# Create the service principal
SP=$(az ad sp create-for-rbac --name "peerisland-cicd" --role Contributor \
  --scopes /subscriptions/<SUB_ID> --query appId -o tsv)

# Add federated credential
az ad app federated-credential create --id "$SP" --parameters '{
  "name": "github-actions",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<org>/<repo>:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

## Deployment environments

The `deploy.yml` workflow targets the **production** GitHub environment, which can require manual approval before proceeding. Set up environment protection rules in `Settings → Environments → production`.

## Rollback

```bash
# List recent image tags in ACR
az acr repository show-tags --name peerislandprodacr --repository backend --orderby time_desc

# Redeploy a specific tag via workflow_dispatch
gh workflow run deploy.yml -f image_tag=<previous_sha>
```
