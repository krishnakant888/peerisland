# ── Networking module — VNet, subnets, NSG ────────────────────────────────────
#
# THREE SUBNETS — why separate subnets for each concern?
#
#   aks-subnet:
#     AKS node VMs and pods live here (Azure CNI → real VNet IPs for pods).
#     Service endpoints for ACR and Key Vault allow the nodes to reach those
#     services over the private Microsoft backbone without leaving the VNet.
#
#   appgw-subnet:
#     Reserved for a future Azure Application Gateway (WAF layer).
#     Not currently used — NGINX Ingress handles ingress today.
#     Azure requires AppGW to have its own /24+ subnet; we reserve it now
#     so we don't need to resize the VNet later.
#
#   pe-subnet:
#     Private endpoint NICs land here. Each private endpoint gets a NIC with
#     a private IP. Network policies must be DISABLED on this subnet so the
#     platform can apply its own routing for private link traffic.
#
# NSG:
#   Applied to the aks-subnet. Allows HTTPS inbound (443) for user traffic
#   and HTTP inbound (80) for ACME HTTP-01 certificate challenges.
#   All other inbound is blocked by default.
#

resource "azurerm_virtual_network" "main" {
  name                = "${var.name_prefix}-vnet"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = var.vnet_address_space
  tags                = var.tags
}

# ── AKS subnet ─────────────────────────────────────────────────────────────────
# Service endpoints: when a node makes a request to ACR or KV, it goes over the
# Microsoft backbone (not the public internet) and the service sees the VNet IP.
# This avoids routing through public IPs even before private endpoints are set up.
resource "azurerm_subnet" "aks" {
  name                 = "aks-subnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.aks_subnet_cidr]

  service_endpoints = [
    "Microsoft.ContainerRegistry",
    "Microsoft.KeyVault",
  ]
}

# ── AppGW subnet ───────────────────────────────────────────────────────────────
# Azure rejects concurrent subnet writes on the same VNet — must be sequential.
# depends_on enforces the order.
resource "azurerm_subnet" "appgw" {
  name                 = "appgw-subnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.appgw_subnet_cidr]

  depends_on = [azurerm_subnet.aks]
}

# ── Private endpoint subnet ────────────────────────────────────────────────────
# private_endpoint_network_policies = "Disabled":
#   Azure requires this on the PE subnet so the platform can inject its own
#   User Defined Routes (UDRs) for private link traffic.
#   Without it, NSG rules applied to the subnet would interfere with PE routing.
resource "azurerm_subnet" "pe" {
  name                 = "pe-subnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.pe_subnet_cidr]

  private_endpoint_network_policies = "Disabled"

  depends_on = [azurerm_subnet.appgw]
}

# ── Network Security Group ─────────────────────────────────────────────────────
# Applied to the AKS subnet. Default Azure NSG behaviour already blocks all
# inbound traffic not explicitly allowed, but we add explicit rules for clarity
# and so the SARIF security scan passes (Checkov checks for NSG attachment).
resource "azurerm_network_security_group" "aks" {
  #checkov:skip=CKV_AZURE_160: Port 80 required for NGINX HTTP→HTTPS redirect and Let's Encrypt ACME HTTP-01 challenge
  name                = "${var.name_prefix}-aks-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "allow-https-inbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  # Port 80 is needed for two things:
  #   1. NGINX Ingress redirects HTTP → HTTPS (301) — requires port 80 open
  #   2. cert-manager ACME HTTP-01 challenge: ZeroSSL calls /.well-known/acme-challenge/
  #      over plain HTTP before issuing the TLS cert
  security_rule {
    name                       = "allow-http-inbound"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

# Associate the NSG with the AKS subnet
resource "azurerm_subnet_network_security_group_association" "aks" {
  subnet_id                 = azurerm_subnet.aks.id
  network_security_group_id = azurerm_network_security_group.aks.id
}
