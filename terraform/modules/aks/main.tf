resource "random_string" "system_node_pool_tmp" {
  length  = 11   # prefixed with "t" below → 12 chars total, always starts with letter
  special = false
  upper   = false
  numeric = true

  # Generates a new temporary name when a rotation-triggering attribute changes
  keepers = {
    vm_size        = var.system_node_pool_vm_size
    vnet_subnet_id = var.node_subnet_id
    os_disk_size   = 128
    os_disk_type   = "Managed"
  }
}

resource "azurerm_kubernetes_cluster" "main" {
  #checkov:skip=CKV_AZURE_115: Public API server, IP-restricted — private cluster needs VPN/jumpbox, incompatible with CI/CD
  #checkov:skip=CKV_AZURE_116: Azure Policy add-on adds cost/complexity for a demo stack
  #checkov:skip=CKV_AZURE_117: Customer-managed disk encryption adds KMS operational overhead
  #checkov:skip=CKV_AZURE_227: Host-based encryption requires EncryptionAtHost feature flag per subscription
  #checkov:skip=CKV_AZURE_226: Ephemeral OS disks unsupported on D2s_v5 with 128GB OS disk size
  #checkov:skip=CKV_AZURE_170: Paid (Uptime SLA) tier not required for a demo/interview stack
  #checkov:skip=CKV_AZURE_168: max_pods set to AKS default (30 system pool); user pool has 110
  #checkov:skip=CKV_AZURE_232: only_critical_addons_enabled would block LGTM/app workloads on the system pool
  #checkov:skip=CKV_AZURE_171: Upgrade channel managed manually — auto-upgrade not suitable for a pinned k8s version
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
    name                         = "system"
    vm_size                      = var.system_node_pool_vm_size
    temporary_name_for_rotation  = "t${random_string.system_node_pool_tmp.result}"
    auto_scaling_enabled         = true
    min_count                    = var.node_pool_min_count
    max_count                    = var.node_pool_max_count
    vnet_subnet_id               = var.node_subnet_id
    type                         = "VirtualMachineScaleSets"
    os_disk_size_gb              = 128
    os_disk_type                 = "Managed"
    only_critical_addons_enabled = false

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

  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

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

# Grant Network Contributor on the AKS node resource group (MC_*) so the
# cloud-controller-manager can add NodePort rules to the AKS-managed NSG
# (aks-agentpool-*-nsg) when LoadBalancer services are provisioned.
resource "azurerm_role_assignment" "aks_node_rg_network_contributor" {
  principal_id                     = azurerm_kubernetes_cluster.main.identity[0].principal_id
  role_definition_name             = "Network Contributor"
  scope                            = "/subscriptions/${data.azurerm_subscription.current.subscription_id}/resourceGroups/${azurerm_kubernetes_cluster.main.node_resource_group}"
  skip_service_principal_aad_check = true
}

data "azurerm_subscription" "current" {}

# The KV secrets provider managed identity is auto-created by AKS in the MC_ resource group.
# We add a federated credential so the monitoring-grafana SA can exchange its token for KV access.
data "azurerm_user_assigned_identity" "kv_secrets_provider" {
  name                = "azurekeyvaultsecretsprovider-${azurerm_kubernetes_cluster.main.name}"
  resource_group_name = azurerm_kubernetes_cluster.main.node_resource_group

  depends_on = [azurerm_kubernetes_cluster.main]
}

resource "azurerm_federated_identity_credential" "grafana_kv" {
  name                        = "grafana-workload-identity"
  user_assigned_identity_id   = data.azurerm_user_assigned_identity.kv_secrets_provider.id
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:monitoring:monitoring-grafana"
  audience            = ["api://AzureADTokenExchange"]

  lifecycle {
    ignore_changes = [user_assigned_identity_id]
  }
}

# Find all NSGs that AKS created in the node resource group.
# Note: on a brand-new cluster this data source resolves after the node pool
# is ready. Run `terraform apply` twice on the very first deployment if the
# plan shows "values not yet known" for the NSG rule resources.
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


resource "random_string" "user_node_pool_tmp" {
  length  = 11   # prefixed with "t" below → 12 chars total, always starts with letter
  special = false
  upper   = false
  numeric = true

  # Regenerates a new temporary name when a rotation-triggering attribute changes
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
  max_pods                    = 110
  vnet_subnet_id              = var.node_subnet_id
  os_disk_type                = "Managed"
  os_disk_size_gb             = 128
  mode                        = "User"
  tags                        = var.tags

  node_labels = {
    "nodepool-type" = "user"
    "environment"   = "production"
  }

  upgrade_settings {
    max_surge = "10%"
  }
}
