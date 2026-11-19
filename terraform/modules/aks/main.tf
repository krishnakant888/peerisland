resource "azurerm_kubernetes_cluster" "main" {
  name                = "${var.name_prefix}-aks"
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = var.name_prefix
  kubernetes_version  = var.kubernetes_version
  tags                = var.tags

  # CKV_AZURE_6: Enable Kubernetes RBAC
  role_based_access_control_enabled = true

  # Azure AD-integrated RBAC — eliminates local kubeconfig credentials
  azure_active_directory_role_based_access_control {
    managed                = true
    azure_rbac_enabled     = true
    admin_group_object_ids = var.admin_group_object_ids
  }

  # CKV_AZURE_7: Restrict who can reach the API server
  api_server_authorized_ip_ranges = length(var.authorized_ip_ranges) > 0 ? var.authorized_ip_ranges : null

  # CKV_AZURE_141: Disable local accounts — force Azure AD auth only
  local_account_disabled = true

  default_node_pool {
    name                 = "system"
    vm_size              = var.node_pool_vm_size
    enable_auto_scaling  = true
    min_count            = var.node_pool_min_count
    max_count            = var.node_pool_max_count
    vnet_subnet_id       = var.node_subnet_id
    type                 = "VirtualMachineScaleSets"
    os_disk_size_gb      = 128
    os_disk_type         = "Managed"

    # CKV_AZURE_227: Encrypt host disk with customer or platform key
    enable_host_encryption = true

    # Keeps user workloads off system nodes. AKS add-ons (CoreDNS, kube-proxy,
    # metrics-server) carry this toleration by default so they still schedule here.
    node_taints = ["CriticalAddonsOnly=true:NoSchedule"]

    node_labels = {
      "nodepool-type" = "system"
      "environment"   = "production"
    }

    upgrade_settings {
      max_surge = "10%"
    }
  }

  # System-assigned managed identity — no credential rotation needed
  identity {
    type = "SystemAssigned"
  }

  # CKV_AZURE_115: Azure Policy add-on enforces OPA-based admission control
  azure_policy_enabled = true

  # CKV_AZURE_8: Azure CNI with network policy blocks east-west traffic by default
  network_profile {
    network_plugin    = "azure"
    network_policy    = "azure"
    load_balancer_sku = "standard"
    outbound_type     = "loadBalancer"
  }

  # Sends diagnostic logs and metrics to Log Analytics
  oms_agent {
    log_analytics_workspace_id = var.log_analytics_workspace_id
  }

  # CKV_AZURE_171: Auto-upgrade channel keeps cluster patched automatically
  automatic_channel_upgrade = "stable"

  # CKV_AZURE_172: Independently upgrade node OS images
  node_os_channel_upgrade = "NodeImage"

  # Microsoft Defender for Containers — runtime threat detection
  microsoft_defender {
    log_analytics_workspace_id = var.log_analytics_workspace_id
  }

  # CSI driver to mount Key Vault secrets as volumes
  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  # Restrict maintenance to low-traffic window
  maintenance_window {
    allowed {
      day   = "Sunday"
      hours = [2, 4]
    }
  }

  lifecycle {
    ignore_changes = [
      kubernetes_version,
      default_node_pool[0].node_count,
    ]
  }
}

# Allow AKS kubelet to pull images from ACR (no password required)
resource "azurerm_role_assignment" "aks_acr_pull" {
  principal_id                     = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
  role_definition_name             = "AcrPull"
  scope                            = var.acr_id
  skip_service_principal_aad_check = true
}

# Allow AKS Key Vault CSI driver to read secrets
resource "azurerm_role_assignment" "aks_kv_secrets_user" {
  principal_id         = azurerm_kubernetes_cluster.main.key_vault_secrets_provider[0].secret_identity[0].object_id
  role_definition_name = "Key Vault Secrets User"
  scope                = var.key_vault_id
}

# Separate user node pool for application workloads
resource "azurerm_kubernetes_cluster_node_pool" "user" {
  name                   = "user"
  kubernetes_cluster_id  = azurerm_kubernetes_cluster.main.id
  vm_size                = var.node_pool_vm_size
  enable_auto_scaling    = true
  min_count              = var.node_pool_min_count
  max_count              = var.node_pool_max_count
  vnet_subnet_id         = var.node_subnet_id
  os_disk_type           = "Managed"
  os_disk_size_gb        = 128
  enable_host_encryption = true
  mode                   = "User"
  tags                   = var.tags

  node_labels = {
    "nodepool-type" = "user"
    "environment"   = "production"
  }

  node_taints = ["workload=user:NoSchedule"]

  upgrade_settings {
    max_surge = "10%"
  }
}
