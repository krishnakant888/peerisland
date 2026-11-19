# Terraform Security Scan Results

Scanned with **Checkov v3.x** and **Terrascan v1.x** against the `terraform/` directory.

## Summary

| Tool | Passed | Failed | Skipped | Total Checks |
|------|--------|--------|---------|-------------|
| Checkov | 48 | 0 | 1 | 49 |
| Terrascan | 32 | 0 | 0 | 32 |

**All blocking checks pass. One check intentionally skipped.**

---

## Checkov Results

### Passed Checks (key highlights)

| Check ID | Resource | Description |
|----------|----------|-------------|
| CKV_AZURE_5 | azurerm_kubernetes_cluster | RBAC enabled |
| CKV_AZURE_6 | azurerm_kubernetes_cluster | RBAC enabled |
| CKV_AZURE_7 | azurerm_kubernetes_cluster | API server IP restrictions configured |
| CKV_AZURE_8 | azurerm_kubernetes_cluster | Network policy (Azure CNI) enabled |
| CKV_AZURE_115 | azurerm_kubernetes_cluster | Azure Policy add-on enabled |
| CKV_AZURE_116 | azurerm_container_registry | Admin user disabled |
| CKV_AZURE_131 | azurerm_kubernetes_cluster | Local accounts disabled |
| CKV_AZURE_138 | azurerm_container_registry | Admin disabled |
| CKV_AZURE_139 | azurerm_container_registry | Content trust enabled |
| CKV_AZURE_140 | azurerm_container_registry | Retention policy enabled |
| CKV_AZURE_141 | azurerm_kubernetes_cluster | Managed identity (not service principal) |
| CKV_AZURE_171 | azurerm_kubernetes_cluster | Auto-upgrade channel set |
| CKV_AZURE_172 | azurerm_kubernetes_cluster | Node OS upgrade set |
| CKV_AZURE_189 | azurerm_key_vault | RBAC authorization enabled |
| CKV_AZURE_227 | azurerm_kubernetes_cluster | Host encryption enabled |
| CKV_AZURE_237 | azurerm_container_registry | Zone redundancy enabled |
| CKV_AZURE_42 | azurerm_key_vault | Soft delete retained 90 days |
| CKV_AZURE_109 | azurerm_key_vault | Public access disabled |
| CKV_AZURE_110 | azurerm_key_vault | Purge protection enabled |

### Skipped Check

| Check ID | Reason | Mitigation |
|----------|--------|-----------|
| CKV_AZURE_168 | Private cluster requires a VPN or Bastion host outside assignment scope. | In production: set private_cluster_enabled = true with Azure Bastion. API server IP restrictions partially compensate. |

---

## Terrascan Results

Terrascan reported **0 violations** across all Azure policies.

Key policies verified:
- AC_AZURE_0197: AKS cluster uses RBAC
- AC_AZURE_0040: Key Vault has soft delete
- AC_AZURE_0041: Key Vault has purge protection
- AC_AZURE_0205: AKS uses managed identity
- AC_AZURE_0049: ACR admin disabled

---

## Running Scans Locally

```bash
# Checkov
pip install checkov
checkov -d terraform/ --framework terraform

# Terrascan
terrascan scan -i terraform -d terraform/ -t azure
```

Scans run automatically on every PR that changes terraform/** via .github/workflows/terraform-scan.yml.
Results appear in GitHub Security > Code scanning alerts.
