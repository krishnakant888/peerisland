.PHONY: tf-init tf-plan tf-apply tf-destroy scan-checkov scan-terrascan \
        install-monitoring install-argocd build-images k8s-apply help

NAMESPACE    := peerisland
ENV          := prod
TF_DIR       := terraform
OVERLAY      := k8s/overlays/production

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-22s %s\n",$$1,$$2}'

# --- Terraform ---
tf-init: ## terraform init
	cd $(TF_DIR) && terraform init

tf-plan: ## terraform plan
	cd $(TF_DIR) && terraform plan -var-file=terraform.tfvars -out=tfplan

tf-apply: ## terraform apply (requires tfplan)
	cd $(TF_DIR) && terraform apply tfplan

tf-destroy: ## terraform destroy
	cd $(TF_DIR) && terraform destroy -var-file=terraform.tfvars

# --- Security Scanning ---
scan-checkov: ## Run Checkov against Terraform code
	checkov -d $(TF_DIR) --framework terraform --config-file .checkov.yml

scan-terrascan: ## Run Terrascan against Terraform code
	terrascan scan -i terraform -d $(TF_DIR) -t azure

scan-all: scan-checkov scan-terrascan ## Run all security scans

# --- Monitoring ---
install-monitoring: ## Install LGTM stack + Prometheus adapter via Helm
	bash monitoring/install.sh

install-prometheus-adapter: ## Install only the Prometheus adapter (custom metrics for HPA)
	helm upgrade --install prometheus-adapter prometheus-community/prometheus-adapter \
	  --namespace monitoring \
	  --values monitoring/prometheus-adapter-values.yaml --wait

check-custom-metrics: ## Verify HPA custom metrics are available
	@echo "=== Registered custom metrics ===" && \
	  kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 | python3 -m json.tool | grep '"name"'
	@echo ""
	@echo "=== HPA status ===" && kubectl get hpa -n peerisland

# --- ArgoCD ---
install-argocd: ## Install ArgoCD via Helm and register app
	bash argocd/install-argocd.sh

install-vpa: ## Install VPA CRDs and components (Recommender, Updater, Admission Controller)
	bash k8s/autoscaling/install-vpa.sh

vpa-recommendations: ## Show current VPA resource recommendations
	@echo "=== backend VPA ===" && kubectl describe vpa backend -n peerisland
	@echo "=== frontend VPA ===" && kubectl describe vpa frontend -n peerisland

# --- Local development ---
build-images: ## Build Docker images locally
	docker build -t peerisland/backend:local  app/backend
	docker build -t peerisland/frontend:local app/frontend

run-local: ## Run app locally with Docker Compose (requires docker-compose)
	docker compose -f docker-compose.yml up

# --- Kubernetes (manual apply, bypass GitOps — use sparingly) ---
k8s-apply: ## kubectl apply the production overlay directly
	kubectl apply -k $(OVERLAY)

k8s-diff: ## Preview what would change
	kubectl diff -k $(OVERLAY)

daily-report: ## Trigger the daily report CronJob manually
	kubectl create job --from=cronjob/daily-report manual-report-$$(date +%s) -n monitoring
