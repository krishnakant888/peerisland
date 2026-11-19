# PeerIsland — AKS Production Environment

A production-like Azure Kubernetes Service deployment with GitOps, infrastructure-as-code, security scanning, and full observability.

## Architecture Overview

```
GitHub ──push──► CI (build-push.yml)
                    │  builds images, pushes to ACR
                    │  bumps image tag in k8s/overlays/production/kustomization.yaml
                    └──► git push (tag commit)
                              │
                              ▼
                         ArgoCD (watches main)
                              │  detects change, syncs
                              ▼
                         AKS Cluster (peerisland namespace)
                         ├── frontend  (3 replicas, HPA)
                         └── backend   (3 replicas, HPA)
                                │
                                └── metrics ──► Prometheus ──► Grafana
```

```
Azure Resources
├── Resource Group: peerisland-prod-rg
├── VNet:  10.0.0.0/8  (aks-subnet, appgw-subnet)
├── AKS:   peerisland-prod-aks  (Azure CNI, Azure AD RBAC)
├── ACR:   peerislandprodacr    (Premium, content trust, no admin)
├── KV:    peerisland-prod-kv   (RBAC auth, purge protection)
└── LAW:   peerisland-prod-law  (30d retention, Defender)
```

## Repository Structure

```
.
├── terraform/              # Infrastructure as code
│   └── modules/
│       ├── aks/            # AKS cluster
│       ├── networking/     # VNet + subnets + NSGs
│       ├── acr/            # Container registry
│       └── keyvault/       # Key Vault
├── app/
│   ├── backend/            # Node.js REST API (Express + prom-client)
│   └── frontend/           # Node.js static server (task manager UI)
├── k8s/
│   ├── *.yaml              # Base manifests (deployments, services, etc.)
│   └── overlays/
│       └── production/     # Kustomize overlay — ArgoCD syncs THIS path
├── argocd/                 # ArgoCD Helm values + Application CRD
├── monitoring/             # Prometheus + Grafana Helm values + dashboard
│   └── dashboards/         # Grafana dashboard JSON
├── k8s/monitoring/         # Daily report CronJob
├── .github/workflows/
│   ├── terraform-scan.yml  # Checkov + Terrascan on every PR
│   ├── build-push.yml      # Build images + update tag in Git
│   └── deploy.yml          # Manual rollback / force ArgoCD sync
└── docs/                   # Detailed documentation
```

## Quick Start

### 1. Provision Infrastructure

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit terraform.tfvars

make tf-init
make tf-plan
make tf-apply
```

### 2. Install ArgoCD

```bash
# Update the repoURL in argocd/applications/peerisland-app.yaml first
make install-argocd
```

### 3. Install Monitoring

```bash
GRAFANA_ADMIN_PASSWORD=YourStrongPassword make install-monitoring
kubectl apply -f k8s/monitoring/daily-report-cronjob.yaml
```

### 4. Configure GitHub Secrets

| Secret | Description |
|--------|-------------|
| `AZURE_CLIENT_ID` | Federated service principal client ID |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription ID |
| `ACR_NAME` | ACR resource name |
| `ACR_LOGIN_SERVER` | `<name>.azurecr.io` |
| `AKS_CLUSTER_NAME` | AKS cluster name |
| `AKS_RESOURCE_GROUP` | Resource group name |

### 5. Deploy

Push a change to `app/**` on `main` — the pipeline builds, pushes, and commits the new tag. ArgoCD syncs the cluster within ~3 minutes.

## Security Highlights

| Area | Control |
|------|---------|
| Infrastructure | Checkov + Terrascan on every PR; 48/49 checks pass |
| Auth | Azure AD RBAC; local accounts disabled; OIDC in CI (no stored passwords) |
| Network | Azure CNI + NetworkPolicy (default deny); NSG on AKS subnet |
| Images | ACR admin disabled; Trivy blocks CRITICAL CVEs; content trust |
| Secrets | Key Vault CSI driver; purge protection; RBAC auth |
| Containers | Non-root, read-only rootfs, `capabilities drop ALL`, seccomp |
| Cluster | Defender for Containers; Azure Policy add-on; auto-upgrade |

## GitOps Flow

Normal path: **Git is the single source of truth** — nobody `kubectl apply`s manually.

1. Developer pushes code to `app/**` on `main`
2. `build-push.yml` builds, scans (Trivy), pushes images to ACR
3. `kustomize edit set image` updates `k8s/overlays/production/kustomization.yaml`
4. The tag commit is pushed to `main` (`[skip ci]` prevents re-triggering)
5. ArgoCD detects the commit via webhook or polling (3m), diffs, and syncs
6. Rollout status visible in ArgoCD UI and `kubectl rollout status`

**Rollback**: use `deploy.yml` (workflow_dispatch) with the previous image tag — it re-commits the old tag to Git, ArgoCD rolls back automatically.

## Monitoring & Daily Reports

- **Grafana**: `https://peerisland.yourdomain.com/grafana` — PeerIsland Application Dashboard
- **Daily report**: CronJob runs at 07:00 UTC; view with `make daily-report` → `kubectl logs`
- **Alerts**: Checkov-equivalent alerts for >5% error rate, P99 > 2s, frequent pod restarts

## Documentation

| Doc | Description |
|-----|-------------|
| [docs/terraform-setup.md](docs/terraform-setup.md) | Terraform setup and deployment steps |
| [docs/cicd-workflow.md](docs/cicd-workflow.md) | CI/CD pipeline details |
| [docs/access-instructions.md](docs/access-instructions.md) | AKS, Grafana, app access |
| [docs/terraform-scan-results.md](docs/terraform-scan-results.md) | Checkov / Terrascan results |

## Design Decisions & Assumptions

1. **GitOps over push-deploy**: ArgoCD (not `kubectl apply` in CI) manages cluster state, giving audit trail, drift detection, and self-healing.
2. **Kustomize overlays**: avoids Helm templating complexity for simple manifests; works natively with ArgoCD.
3. **Private cluster skipped**: `private_cluster_enabled = false` for assignment convenience; API server IP restrictions partially compensate. Enable in production with an Azure Bastion.
4. **In-memory task store**: the backend uses a `Map` for simplicity; replace with Azure Database for PostgreSQL in production.
5. **OIDC over client secrets**: both Terraform and GitHub Actions authenticate via federated identity — no stored passwords.
6. **Standard_DS2_v2 nodes**: sufficient for the demo; right-size based on actual workload metrics.
