resource "azurerm_kubernetes_cluster" "main" {
  name                = "${var.name_prefix}-aks"
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = var.name_prefix
  kubernetes_version  = var.kubernetes_version
  tags                = var.tags

  role_based_access_control_enabled = true

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled     = true
    admin_group_object_ids = var.admin_group_object_ids
  }

  local_account_disabled = true

  api_server_access_profile {
    authorized_ip_ranges = length(var.authorized_ip_ranges) > 0 ? var.authorized_ip_ranges : ["0.0.0.0/0"]
  }

  default_node_pool {
    name                        = "system"
    temporary_name_for_rotation = "systmp"
    vm_size                     = var.system_node_pool_vm_size
    auto_scaling_enabled        = true
    min_count                   = var.node_pool_min_count
    max_count                   = var.node_pool_max_count
    vnet_subnet_id              = var.node_subnet_id
    type                        = "VirtualMachineScaleSets"
    os_disk_size_gb             = 128
    os_disk_type                = "Managed"
    only_critical_addons_enabled = true

    node_labels = {
      "nodepool-type" = "system"
      "environment"   = "production"
    }

    upgrade_settings {
      max_surge = "10%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin    = "azure"
    network_policy    = "azure"
    load_balancer_sku = "standard"
    outbound_type     = "loadBalancer"
  }

  oms_agent {
    log_analytics_workspace_id = var.log_analytics_workspace_id
  }

  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  lifecycle {
    ignore_changes = [
      kubernetes_version,
      default_node_pool[0].node_count,
      default_node_pool[0].max_pods,
    ]
  }
}

resource "azurerm_role_assignment" "aks_acr_pull" {
  principal_id                     = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
  role_definition_name             = "AcrPull"
  scope                            = var.acr_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "aks_kv_secrets_user" {
  principal_id         = azurerm_kubernetes_cluster.main.key_vault_secrets_provider[0].secret_identity[0].object_id
  role_definition_name = "Key Vault Secrets User"
  scope                = var.key_vault_id
}

resource "azurerm_role_assignment" "aks_vnet_network_contributor" {
  principal_id                     = azurerm_kubernetes_cluster.main.identity[0].principal_id
  role_definition_name             = "Network Contributor"
  scope                            = var.vnet_id
  skip_service_principal_aad_check = true
}

resource "azurerm_kubernetes_cluster_node_pool" "user" {
  name                        = "user"
  temporary_name_for_rotation = "usertmp"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = var.node_pool_vm_size
  auto_scaling_enabled  = true
  min_count             = var.user_node_pool_min_count
  max_count             = var.user_node_pool_max_count
  max_pods              = 110
  vnet_subnet_id        = var.node_subnet_id
  os_disk_type          = "Managed"
  os_disk_size_gb       = 128
  mode                  = "User"
  tags                  = var.tags

  node_labels = {
    "nodepool-type" = "user"
    "environment"   = "production"
  }

  upgrade_settings {
    max_surge = "10%"
  }
}
