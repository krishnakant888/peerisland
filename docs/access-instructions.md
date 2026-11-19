# Access Instructions

## AKS Cluster

### Prerequisites
- Azure CLI installed and logged in: `az login`
- kubectl installed: https://kubernetes.io/docs/tasks/tools/

### Configure kubectl

```bash
az aks get-credentials \
  --resource-group peerisland-prod-rg \
  --name peerisland-prod-aks \
  --overwrite-existing

# Verify connection
kubectl get nodes
kubectl get pods -n peerisland
```

> Authentication is via **Azure AD** — the user/group must have an RBAC role assignment on the AKS cluster (`Azure Kubernetes Service RBAC Reader` minimum, `Cluster Admin` for full access).

### Granting access to a user

```bash
az role assignment create \
  --assignee <user-object-id> \
  --role "Azure Kubernetes Service RBAC Reader" \
  --scope /subscriptions/<SUB>/resourceGroups/peerisland-prod-rg/providers/Microsoft.ContainerService/managedClusters/peerisland-prod-aks
```

---

## Sample Application

### Production URL

```
https://peerisland.yourdomain.com
```

Configure the DNS A record to point to the NGINX ingress controller's public IP:

```bash
kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

### Local access (port-forward)

```bash
# Frontend
kubectl port-forward svc/frontend 3000:80 -n peerisland
# → http://localhost:3000

# Backend API
kubectl port-forward svc/backend 3001:80 -n peerisland
# → http://localhost:3001/api/tasks
```

---

## Grafana

### Production URL

```
https://peerisland.yourdomain.com/grafana
```

Default credentials (change immediately):
- **Username**: `admin`
- **Password**: set via `GRAFANA_ADMIN_PASSWORD` during install, stored in K8s secret `grafana` in namespace `monitoring`

```bash
# Retrieve current admin password
kubectl get secret grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d
```

### Local access

```bash
kubectl port-forward svc/grafana 3000:80 -n monitoring
# → http://localhost:3000
```

Navigate to: **Dashboards → PeerIsland → PeerIsland Application Dashboard**

---

## Prometheus

Prometheus is cluster-internal only. Access via port-forward:

```bash
kubectl port-forward svc/prometheus-server 9090:80 -n monitoring
# → http://localhost:9090
```

---

## Daily Report

The `daily-report` CronJob runs at 07:00 UTC and outputs to pod logs:

```bash
# View latest report
kubectl logs -n monitoring -l job-name=daily-report --tail=100

# Trigger manually
kubectl create job --from=cronjob/daily-report manual-report-$(date +%s) -n monitoring
```
