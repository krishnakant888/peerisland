# ── AKS module — cluster, node pools, OIDC, workload identity ─────────────────
#
# TWO NODE POOLS — why separate?
#   system pool ("system"):
#     - Runs kube-system components: CoreDNS, konnectivity, CSI drivers
#     - D2s_v3 (2 vCPU / 8GB) — cheap, kube-system is lightweight
#     - only_critical_addons_enabled = false — we allow LGTM/app pods here
#       if the user pool is full (not ideal but prevents OOM evictions)
#
#   user pool ("user"):
#     - Runs all application workloads: frontend, backend, Alloy, Mimir, Loki, Tempo
#     - D4s_v5 (4 vCPU / 16GB) — minimum for LGTM stack + app pods
#     - Autoscales: 2–5 nodes (user_node_pool_min/max_count in variables.tf)
#
# WORKLOAD IDENTITY + OIDC:
#   oidc_issuer_enabled + workload_identity_enabled enables the OIDC token issuer.
#   Pods in the monitoring namespace get a Kubernetes SA token that Azure trusts.
#   Grafana uses this to fetch secrets from Key Vault without any stored credentials.
#   The federated identity credential (grafana_kv below) ties the SA to the AKS
#   KV secrets provider managed identity.
#
# NODE ROTATION (temporary_name_for_rotation):
#   Changing VM size or subnet requires destroying and recreating the node pool.
#   AzureRM does this by creating a temporary pool, migrating pods, then deleting the old.
#   The random_string "keeps" (keepers) regenerate the name when rotation-triggering
#   attributes change, so Terraform automatically gets a new temporary name.
#

resource "random_string" "system_node_pool_tmp" {
  length  = 11   # prefixed with "t" below → 12 chars total, must start with letter
  special = false
  upper   = false
  numeric = true

  # Regenerate the temporary name when any rotation-triggering attribute changes.
  # Terraform tracks this: if keepers change → new random string → new temp pool name.
  keepers = {
    vm_size        = var.system_node_pool_vm_size
    vnet_subnet_id = var.node_subnet_id
    os_disk_size   = 128
    os_disk_type   = "Managed"
  }
}

resource "azurerm_kubernetes_cluster" "main" {
  # checkov skips explained — these are intentional trade-offs for a demo/assignment stack:
  #checkov:skip=CKV_AZURE_115: Public API server, IP-restricted — private cluster needs VPN/jumpbox
  #checkov:skip=CKV_AZURE_116: Azure Policy add-on adds cost/complexity for a demo stack
  #checkov:skip=CKV_AZURE_117: Customer-managed disk encryption adds KMS operational overhead
  #checkov:skip=CKV_AZURE_227: Host-based encryption requires EncryptionAtHost feature flag per subscription
  #checkov:skip=CKV_AZURE_226: Ephemeral OS disks unsupported on D2s_v5 with 128GB OS disk size
  #checkov:skip=CKV_AZURE_170: Paid (Uptime SLA) tier not required for a demo/interview stack
  #checkov:skip=CKV_AZURE_168: max_pods set to AKS default; custom value needs subnet resize
  #checkov:skip=CKV_AZURE_232: only_critical_addons_enabled would block LGTM/app workloads
  #checkov:skip=CKV_AZURE_171: Upgrade channel managed manually — auto-upgrade not suitable here

  name                = "${var.name_prefix}-aks"
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = var.name_prefix
  kubernetes_version  = var.kubernetes_version
  tags                = var.tags

  role_based_access_control_enabled = true

  # Azure AD RBAC: cluster access is controlled via Azure AD group membership.
  # local_account_disabled = true means the built-in admin kubeconfig is disabled —
  # all access must go through AAD.
  azure_active_directory_role_based_access_control {
    azure_rbac_enabled     = true
    admin_group_object_ids = var.admin_group_object_ids  # the aks-admins AAD group
  }

  # Disables the local admin account — prevents bypass of AAD RBAC
  local_account_disabled = true

  # Restrict who can reach the Kubernetes API server.
  # In prod this should be CI/CD runner IPs + operator workstation.
  # Defaults to 0.0.0.0/0 if var.authorized_ip_ranges is empty.
  api_server_access_profile {
    authorized_ip_ranges = length(var.authorized_ip_ranges) > 0 ? var.authorized_ip_ranges : ["0.0.0.0/0"]
  }

  # ── System node pool ────────────────────────────────────────────────────────
  default_node_pool {
    name    = "system"
    vm_size = var.system_node_pool_vm_size

    # temporary_name_for_rotation: AzureRM uses this name for the swap pool
    # during node pool updates that require replacement (VM size change, subnet change).
    temporary_name_for_rotation = "t${random_string.system_node_pool_tmp.result}"

    auto_scaling_enabled = true
    min_count            = var.node_pool_min_count
    max_count            = var.node_pool_max_count
    vnet_subnet_id       = var.node_subnet_id
    type                 = "VirtualMachineScaleSets"
    os_disk_size_gb      = 128
    os_disk_type         = "Managed"

    # false = allow non-critical workloads on system pool as a fallback
    only_critical_addons_enabled = false

    node_labels = {
      "nodepool-type" = "system"
      "environment"   = "production"
    }

    upgrade_settings {
      max_surge = "10%"  # allow 10% extra nodes during k8s version upgrades
    }
  }

  # SystemAssigned identity: AKS creates a managed identity automatically.
  # This identity is used for VNET/NSG operations (Network Contributor role).
  identity {
    type = "SystemAssigned"
  }

  # Azure CNI: each pod gets a real VNet IP (not an overlay network IP).
  # Required for private endpoints and network policies to work correctly.
  network_profile {
    network_plugin    = "azure"
    network_policy    = "azure"   # Azure-native NetworkPolicy enforcement
    load_balancer_sku = "standard"
    outbound_type     = "loadBalancer"
  }

  # Stream AKS metrics and container logs to Log Analytics
  oms_agent {
    log_analytics_workspace_id = var.log_analytics_workspace_id
  }

  # ── Workload Identity + OIDC ─────────────────────────────────────────────────
  # oidc_issuer_enabled: AKS becomes an OIDC provider for pod service accounts.
  # workload_identity_enabled: pods can be annotated to exchange their SA token
  # for an Azure AD token (and thus access Azure resources like Key Vault).
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  # ── CSI Secrets Store Provider ──────────────────────────────────────────────
  # Mounts Key Vault secrets as Kubernetes Secret objects via SecretProviderClass.
  # secret_rotation_interval: re-fetches KV secrets every 2 minutes.
  # If a secret is rotated in KV, the k8s Secret and Grafana pod env vars
  # are updated within 2 minutes automatically.
  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  # ── Lifecycle ignores ────────────────────────────────────────────────────────
  # kubernetes_version: AKS upgrades the cluster version out-of-band (node OS patches).
  # We pin k8s version via var.kubernetes_version; ignore drift from AKS auto-patching.
  # node_count: HPA/KEDA may change node count; ignore to prevent Terraform conflicts.
  # max_pods: AKS sets this per pool; we don't want to drift-detect it.
  # vm_size: planned rotation in progress — ignore until a maintenance window.
  lifecycle {
    ignore_changes = [
      kubernetes_version,
      default_node_pool[0].node_count,
      default_node_pool[0].max_pods,
      # System pool was provisioned as D4s_v5; rotating to D2s_v3 requires a
      # temporary pool which may fail in this region — ignore until planned.
      default_node_pool[0].vm_size,
    ]
  }
}

# ── Role assignments ────────────────────────────────────────────────────────────

# Allow AKS to pull images from ACR without storing credentials.
# kubelet_identity is the managed identity used by all AKS nodes.
resource "azurerm_role_assignment" "aks_acr_pull" {
  principal_id                     = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
  role_definition_name             = "AcrPull"
  scope                            = var.acr_id
  skip_service_principal_aad_check = true
}

# Allow the CSI secrets provider to read secrets from Key Vault.
# This identity is auto-created by AKS — secret_identity[0] is it.
resource "azurerm_role_assignment" "aks_kv_secrets_user" {
  principal_id         = azurerm_kubernetes_cluster.main.key_vault_secrets_provider[0].secret_identity[0].object_id
  role_definition_name = "Key Vault Secrets User"
  scope                = var.key_vault_id
}

# Allow AKS to modify VNet routes and NSG rules (needed for load balancers)
resource "azurerm_role_assignment" "aks_vnet_network_contributor" {
  principal_id                     = azurerm_kubernetes_cluster.main.identity[0].principal_id
  role_definition_name             = "Network Contributor"
  scope                            = var.vnet_id
  skip_service_principal_aad_check = true
}

# AKS creates its own MC_* resource group for node resources.
# The cloud-controller-manager needs Network Contributor there to add
# NodePort rules to the AKS-managed NSG when LoadBalancer services are created.
resource "azurerm_role_assignment" "aks_node_rg_network_contributor" {
  principal_id                     = azurerm_kubernetes_cluster.main.identity[0].principal_id
  role_definition_name             = "Network Contributor"
  scope                            = "/subscriptions/${data.azurerm_subscription.current.subscription_id}/resourceGroups/${azurerm_kubernetes_cluster.main.node_resource_group}"
  skip_service_principal_aad_check = true
}

data "azurerm_subscription" "current" {}

# ── Grafana workload identity federated credential ─────────────────────────────
# The CSI secrets provider managed identity is auto-created by AKS in MC_*.
# We add a federated credential so the monitoring-grafana ServiceAccount can
# exchange its Kubernetes token for an Azure AD token (without storing credentials).
# Flow: Grafana pod → KSA token → Azure AD OIDC exchange → access token → KV read
data "azurerm_user_assigned_identity" "kv_secrets_provider" {
  name                = "azurekeyvaultsecretsprovider-${azurerm_kubernetes_cluster.main.name}"
  resource_group_name = azurerm_kubernetes_cluster.main.node_resource_group

  depends_on = [azurerm_kubernetes_cluster.main]
}

resource "azurerm_federated_identity_credential" "grafana_kv" {
  name                      = "grafana-workload-identity"
  user_assigned_identity_id = data.azurerm_user_assigned_identity.kv_secrets_provider.id
  issuer                    = azurerm_kubernetes_cluster.main.oidc_issuer_url
  # This subject exactly matches the monitoring-grafana ServiceAccount in k8s
  subject                   = "system:serviceaccount:monitoring:monitoring-grafana"
  audience                  = ["api://AzureADTokenExchange"]

  lifecycle {
    ignore_changes = [user_assigned_identity_id]
  }
}

# ── NSG rules on the AKS node resource group ───────────────────────────────────
# AKS creates its own NSG in the MC_* resource group for node traffic.
# We add HTTP (80) and HTTPS (443) inbound rules so ingress-nginx can receive traffic.
# Note: on a brand-new cluster, run terraform apply TWICE — the first apply
# creates the cluster, the second apply can find and tag the NSG.

data "azurerm_resources" "aks_node_nsgs" {
  type                = "Microsoft.Network/networkSecurityGroups"
  resource_group_name = azurerm_kubernetes_cluster.main.node_resource_group

  depends_on = [azurerm_kubernetes_cluster_node_pool.user]
}

resource "azurerm_network_security_rule" "aks_allow_http" {
  #checkov:skip=CKV_AZURE_160: Port 80 required for NGINX HTTP→HTTPS redirect and Let's Encrypt ACME HTTP-01 challenge
  for_each = { "aks_node_nsg" = true }

  name                        = "Allow-HTTP-Inbound"
  priority                    = 200
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "80"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_kubernetes_cluster.main.node_resource_group
  network_security_group_name = data.azurerm_resources.aks_node_nsgs.resources[0].name

  lifecycle {
    ignore_changes = [network_security_group_name]
  }
}

resource "azurerm_network_security_rule" "aks_allow_https" {
  for_each = { "aks_node_nsg" = true }

  name                        = "Allow-HTTPS-Inbound"
  priority                    = 201
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "443"
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_kubernetes_cluster.main.node_resource_group
  network_security_group_name = data.azurerm_resources.aks_node_nsgs.resources[0].name

  lifecycle {
    ignore_changes = [network_security_group_name]
  }
}

# ── User node pool ─────────────────────────────────────────────────────────────
# All application workloads run here (frontend, backend, Alloy, Mimir, Loki, Tempo).
# D4s_v5 (4 vCPU / 16GB) is the minimum for the LGTM observability stack + app pods.
# Autoscaling: 2–5 nodes based on cluster load.

resource "random_string" "user_node_pool_tmp" {
  length  = 11
  special = false
  upper   = false
  numeric = true

  keepers = {
    vm_size        = var.node_pool_vm_size
    vnet_subnet_id = var.node_subnet_id
    max_pods       = 110
    os_disk_size   = 128
    os_disk_type   = "Managed"
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "user" {
  #checkov:skip=CKV_AZURE_227: Host-based encryption requires EncryptionAtHost feature flag per subscription
  name                        = "user"
  temporary_name_for_rotation = "t${random_string.user_node_pool_tmp.result}"
  kubernetes_cluster_id       = azurerm_kubernetes_cluster.main.id
  vm_size                     = var.node_pool_vm_size
  auto_scaling_enabled        = true
  min_count                   = var.user_node_pool_min_count
  max_count                   = var.user_node_pool_max_count
  max_pods                    = 110   # Azure CNI default for user pools
  vnet_subnet_id              = var.node_subnet_id
  os_disk_type                = "Managed"
  os_disk_size_gb             = 128
  mode                        = "User"   # workloads only — no kube-system components
  tags                        = var.tags

  node_labels = {
    "nodepool-type" = "user"
    "environment"   = "production"
  }

  upgrade_settings {
    max_surge = "10%"
  }
}
