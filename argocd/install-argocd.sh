#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="argocd"
ARGOCD_VERSION="6.7.3"  # Helm chart version

echo "==> Creating ArgoCD namespace"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo "==> Adding ArgoCD Helm repo"
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

echo "==> Installing ArgoCD"
helm upgrade --install argocd argo/argo-cd \
  --namespace "$NAMESPACE" \
  --version "$ARGOCD_VERSION" \
  --values "$(dirname "$0")/argocd-values.yaml" \
  --wait --timeout 10m

echo "==> Retrieving initial admin password"
INITIAL_PWD=$(kubectl -n "$NAMESPACE" get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d)
echo "    ArgoCD initial admin password: $INITIAL_PWD"
echo "    Change it immediately after first login!"

echo ""
echo "==> Registering the GitHub repository"
echo "    Run after updating your repo URL in argocd/applications/peerisland-app.yaml:"
echo ""
echo "    argocd repo add https://github.com/krishnakant888/peerisland.git \\"
echo "      --username <user> --password <token>"
echo ""

echo "==> Applying the Application manifest"
kubectl apply -f "$(dirname "$0")/applications/peerisland-app.yaml"

echo ""
echo "==> Done. ArgoCD will now sync k8s/overlays/production from Git."
echo "    Port-forward: kubectl port-forward svc/argocd-server 8080:443 -n argocd"
echo "    URL: https://argocd.peerisland.yourdomain.com"
