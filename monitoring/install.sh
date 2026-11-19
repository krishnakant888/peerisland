#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="monitoring"
GRAFANA_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-changeme123}"
DIR="$(dirname "$0")"

echo "==> Creating monitoring namespace"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NAMESPACE" kubernetes.io/metadata.name="$NAMESPACE" --overwrite

echo "==> Adding Grafana Helm repo"
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

# ── Mimir (metrics storage) ────────────────────────────────────────────────
echo "==> Installing Mimir"
helm upgrade --install mimir grafana/mimir-distributed \
  --namespace "$NAMESPACE" \
  --values "${DIR}/mimir-values.yaml" \
  --wait --timeout 10m

# ── Loki (log storage) ─────────────────────────────────────────────────────
echo "==> Installing Loki"
helm upgrade --install loki grafana/loki \
  --namespace "$NAMESPACE" \
  --values "${DIR}/loki-values.yaml" \
  --wait --timeout 10m

# ── Alloy (collector — metrics + logs) ────────────────────────────────────
echo "==> Installing Alloy"
helm upgrade --install alloy grafana/alloy \
  --namespace "$NAMESPACE" \
  --values "${DIR}/alloy-values.yaml" \
  --wait --timeout 5m

# ── Prometheus Adapter (custom metrics for HPA) ───────────────────────────
echo "==> Installing Prometheus Adapter"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install prometheus-adapter prometheus-community/prometheus-adapter \
  --namespace "$NAMESPACE" \
  --values "${DIR}/prometheus-adapter-values.yaml" \
  --wait --timeout 5m

# Verify custom metrics are registered
echo "    Waiting 30s for custom metrics API to register..."
sleep 30
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 2>/dev/null | \
  python3 -c "import sys,json; r=json.load(sys.stdin); print(f'    Custom metrics registered: {len(r[\"resources\"])} metrics')" \
  || echo "    (custom metrics API not ready yet — check: kubectl get apiservices)"

# ── Grafana (dashboards + querying) ───────────────────────────────────────
echo "==> Creating Grafana dashboard ConfigMap"
kubectl create configmap grafana-dashboards \
  --from-file="${DIR}/dashboards/app-dashboard.json" \
  --namespace "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl label configmap grafana-dashboards \
  grafana_dashboard=1 \
  --namespace "$NAMESPACE" --overwrite

echo "==> Installing Grafana"
helm upgrade --install grafana grafana/grafana \
  --namespace "$NAMESPACE" \
  --values "${DIR}/grafana-values.yaml" \
  --set adminPassword="$GRAFANA_PASSWORD" \
  --wait --timeout 10m

echo ""
echo "==> Stack installed:"
echo "    Alloy   → scrapes metrics + tails logs"
echo "    Mimir   → stores metrics  (PromQL via Grafana)"
echo "    Loki    → stores logs     (LogQL via Grafana)"
echo "    Grafana → dashboards      (both datasources wired up)"
echo ""
echo "    Grafana admin password: $GRAFANA_PASSWORD"
echo "    Port-forward Grafana: kubectl port-forward svc/grafana 3000:80 -n $NAMESPACE"
