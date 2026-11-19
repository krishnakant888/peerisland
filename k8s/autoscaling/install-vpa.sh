#!/usr/bin/env bash
# Installs Vertical Pod Autoscaler CRDs and components via Helm.
# VPA consists of three components:
#   - Recommender: watches pod metrics, computes resource recommendations
#   - Admission Controller: mutates pod specs on creation (Initial/Auto mode)
#   - Updater: evicts pods so they restart with new resources (Auto mode only)
set -euo pipefail

echo "==> Adding Fairwinds Helm repo (VPA chart)"
helm repo add fairwinds-stable https://charts.fairwinds.com/stable
helm repo update

echo "==> Installing VPA"
helm upgrade --install vpa fairwinds-stable/vpa \
  --namespace kube-system \
  --set recommender.enabled=true \
  --set updater.enabled=true \
  --set admissionController.enabled=true \
  --wait --timeout 5m

echo ""
echo "==> VPA installed. Components:"
echo "    kubectl get pods -n kube-system -l app.kubernetes.io/name=vpa"
echo ""
echo "==> Apply VPA objects:"
echo "    kubectl apply -k k8s/autoscaling/"
echo ""
echo "==> Check recommendations after ~10 min of traffic:"
echo "    kubectl describe vpa backend -n peerisland"
echo "    kubectl describe vpa frontend -n peerisland"
