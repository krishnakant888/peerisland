#!/usr/bin/env python3
"""
bootstrap.py — PeerIsland end-to-end infrastructure bootstrap

Runs every step from zero to production:
  1   Pre-flight checks        (tools + Azure + GitHub auth)
  2   Bootstrap TF remote state (terraform/bootstrap/)
  3   Set GitHub Actions secrets
  4   Provision Azure infra     (terraform apply)
  5   Configure kubectl
  6   Seed Key Vault secrets
  7   Install ArgoCD + App of Apps
  8   Wait for ArgoCD sync waves
  9   Trigger first build       (GitHub Actions)
  10  Watch & promote Rollouts
  11  Print all URLs and credentials

Usage:
  python bootstrap.py               run all phases
  python bootstrap.py --from 5      resume from phase 5
  python bootstrap.py --only 6      run phase 6 only
  python bootstrap.py --dry-run     print commands without executing
  python bootstrap.py --reset       clear saved state, start fresh

Requirements: Python 3.9+ stdlib only. Tools: terraform az kubectl argocd gh git kustomize
"""

import argparse
import base64
import getpass
import shutil
import threading
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT  = Path(__file__).parent.resolve()
STATE_FILE = REPO_ROOT / ".bootstrap-state.json"
LOG_FILE   = REPO_ROOT / "bootstrap.log"

# ── ANSI colours ──────────────────────────────────────────────────────────────

def _init_color() -> bool:
    """Enable ANSI VT processing on Windows; return True if colors work."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # Get current stdout console mode and add ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x4)
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong(0)
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x4)
            return True
        except Exception:
            return False
    return True

_USE_COLOR = _init_color()

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED   = "\033[31m"; GREEN  = "\033[32m"; YELLOW = "\033[33m"
BLUE  = "\033[34m"; CYAN   = "\033[36m"; WHITE  = "\033[37m"

def _c(codes, text): return f"{codes}{text}{RESET}" if _USE_COLOR else text
def red(t):    return _c(RED + BOLD, t)
def green(t):  return _c(GREEN, t)
def yellow(t): return _c(YELLOW, t)
def blue(t):   return _c(BLUE + BOLD, t)
def cyan(t):   return _c(CYAN, t)
def dim(t):    return _c(DIM, t)
def bold(t):   return _c(BOLD, t)

# ── logging ───────────────────────────────────────────────────────────────────

_log_fh = None

def _fh():
    global _log_fh
    if _log_fh is None:
        _log_fh = open(LOG_FILE, "a", encoding="utf-8")
    return _log_fh

ICONS = {"INFO": "·", "OK": "✓", "WARN": "!", "ERR": "✗", "CMD": "$"}
CLRS  = {"INFO": WHITE, "OK": GREEN, "WARN": YELLOW, "ERR": RED + BOLD, "CMD": CYAN}

def log(level: str, msg: str):
    ts   = datetime.now().strftime("%H:%M:%S")
    icon = ICONS.get(level, "·")
    clr  = CLRS.get(level, WHITE)
    print(f"{dim(ts)}  {_c(clr, icon)}  {msg}")
    _fh().write(f"{ts}  [{level:4}]  {msg}\n")
    _fh().flush()

def phase_header(n: int, title: str):
    bar = "─" * 56
    print(f"\n{blue(bar)}")
    print(f"{blue('  Phase')} {bold(str(n))}  {bold(title)}")
    print(f"{blue(bar)}")
    _fh().write(f"\n{'=' * 58}\nPHASE {n}: {title}\n{'=' * 58}\n")

def die(msg: str):
    log("ERR", msg)
    sys.exit(1)

# ── subprocess ────────────────────────────────────────────────────────────────

DRY_RUN = False

def run(
    cmd,
    *,
    capture: bool = False,
    check: bool = True,
    cwd=None,
    env=None,
    input_text: str = None,
) -> subprocess.CompletedProcess:
    display = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    log("CMD", cyan(display))
    _fh().write(f"$ {display}\n")

    if DRY_RUN:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or REPO_ROOT),
        env=env or os.environ.copy(),
        input=input_text,
    )
    for line in (result.stdout or "").splitlines():
        print(f"  {dim(line)}")
        _fh().write(f"  {line}\n")
    for line in (result.stderr or "").splitlines():
        print(f"  {dim(line)}")
        _fh().write(f"  {line}\n")

    if result.returncode != 0:
        # Detect "command not found" (exit 127 on bash, or Windows "not recognized")
        err_out = (result.stderr or "") + (result.stdout or "")
        not_found = (result.returncode == 127 or
                     "not recognized" in err_out.lower() or
                     "is not recognized" in err_out.lower() or
                     "command not found" in err_out.lower())
        if not_found:
            tool = display.split()[0]  # first word of the command is the binary
            if tool in _AUTO_INSTALLABLE and tool not in _verified_tools:
                log("WARN", f"{tool} not found — auto-installing and retrying …")
                _require_tool(tool)
                # retry once
                result = subprocess.run(
                    cmd,
                    shell=isinstance(cmd, str),
                    capture_output=capture,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(cwd or REPO_ROOT),
                    env=env or os.environ.copy(),
                    input=input_text,
                )
        if check and result.returncode != 0:
            die(f"Command failed (exit {result.returncode}): {display}")
    return result


def capture(cmd, **kwargs) -> str:
    return run(cmd, capture=True, **kwargs).stdout.strip()


def _lookup_role_assignment_id(guid_no_dashes: str) -> str:
    """Given a bare 32-hex-char role assignment name, return its full Azure resource ID."""
    fmt = (f"{guid_no_dashes[:8]}-{guid_no_dashes[8:12]}-"
           f"{guid_no_dashes[12:16]}-{guid_no_dashes[16:20]}-{guid_no_dashes[20:]}")
    r = subprocess.run(
        f"az role assignment list --all --query \"[?name=='{fmt}'].id\" -o tsv",
        shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=os.environ.copy(),
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _lookup_federated_credential_import_id(diag_addr: str, cwd: Path) -> str:
    """Return the Terraform import ID for a federated identity credential that
    already exists in Azure AD but is missing from state.

    Import ID format: /applications/{objectId}/federatedIdentityCredentials/{credId}
    diag_addr example: azuread_application_federated_identity_credential.github_main
    """
    _, app_obj_id = _tf_cicd_app_ids()
    if not app_obj_id:
        # Fall back to parsing state show directly
        st = subprocess.run(
            "terraform state show azuread_application.cicd",
            shell=True, capture_output=True, text=True, cwd=str(cwd),
            env=os.environ.copy(),
        )
        for line in st.stdout.splitlines():
            m = re.match(r'\s+id\s+=\s+"([^"]+)"', line)
            if m:
                app_obj_id = m.group(1)
                break
    if not app_obj_id:
        log("WARN", "  _lookup_federated_credential_import_id: app object ID not found")
        return ""

    creds_raw = subprocess.run(
        f"az ad app federated-credential list --id {app_obj_id} -o json",
        shell=True, capture_output=True, text=True, env=os.environ.copy(),
    )
    if creds_raw.returncode != 0 or not creds_raw.stdout.strip():
        log("WARN", f"  Could not list federated credentials for app {app_obj_id[:8]}…")
        return ""

    # Derive the display name: "github_main" → "github-main"
    local_name = diag_addr.split(".")[-1].replace("_", "-")
    for cred in json.loads(creds_raw.stdout or "[]"):
        if cred.get("name") == local_name:
            return f"/applications/{app_obj_id}/federatedIdentityCredentials/{cred['id']}"

    log("WARN", f"  No federated credential named '{local_name}' found in Azure AD")
    return ""




def tf_apply(plan_file: str, cwd: Path) -> list[tuple[str, str]]:
    """Stream terraform apply with live progress display.

    Returns a list of (tf_addr, azure_resource_id) pairs for resources that
    already exist in Azure but are missing from Terraform state, so the caller
    can import them and retry.  Returns [] on success.  Calls sys.exit(1) on
    any other failure.
    """
    cmd = f"terraform apply -json {plan_file}"
    log("CMD", cyan(cmd))
    _fh().write(f"$ {cmd}\n")

    if DRY_RUN:
        return []

    in_progress: dict[str, float] = {}
    created, updated, destroyed = [], [], []
    already_exists: list[tuple[str, str]] = []

    action_sym = {
        "create": green("+ create"),
        "update": yellow("~ update"),
        "delete": red("- destroy"),
        "read":   cyan("⟳ read"),
    }

    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(cwd), env=os.environ.copy(), bufsize=1,
    )

    for raw in proc.stdout:
        raw = raw.rstrip()
        _fh().write(raw + "\n")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            if raw.strip():
                print(f"  {dim(raw)}")
            continue

        level   = msg.get("@level", "info")
        text    = msg.get("@message", "")
        mtype   = msg.get("type", "")
        hook    = msg.get("hook", {}) or {}
        res     = hook.get("resource", {}) or {}
        addr    = res.get("addr", "")
        action  = hook.get("action", "")
        elapsed = int(hook.get("elapsed_seconds", 0))

        if mtype == "apply_start":
            in_progress[addr] = time.time()
            print(f"  {action_sym.get(action, cyan('⟳'))}  {addr}")

        elif mtype == "apply_progress":
            print(f"  {action_sym.get(action, cyan('⟳'))}  {addr}  {dim(f'[{elapsed}s elapsed]')}")

        elif mtype == "apply_complete":
            dur = int(time.time() - in_progress.pop(addr, time.time()))
            done = green("✓") if action in ("create", "read") else yellow("✓") if action == "update" else red("✓")
            print(f"  {done} {action_sym.get(action, cyan('⟳'))}  {addr}  {dim(f'({dur}s)')}")
            if   action == "create":  created.append(addr)
            elif action == "update":  updated.append(addr)
            elif action == "delete":  destroyed.append(addr)

        elif mtype == "apply_errored":
            print(f"  {red('✗')} {addr}  {red('FAILED')}")

        elif mtype == "change_summary":
            changes = msg.get("changes", {})
            print(f"\n  {bold('Apply complete!')}  "
                  f"{green(str(changes.get('add', 0)) + ' added')}  "
                  f"{yellow(str(changes.get('change', 0)) + ' changed')}  "
                  f"{red(str(changes.get('remove', 0)) + ' destroyed')}")

        elif mtype == "diagnostic":
            diag     = msg.get("diagnostic", {}) or {}
            # terraform puts the TF resource address directly in diag["address"]
            diag_addr = diag.get("address", "")
            # detail is often empty; fall back through summary then @message
            detail   = (diag.get("detail", "") or diag.get("summary", "") or text)
            if level == "error":
                print(f"  {red('ERR')} {detail[:300]}")
                if diag_addr:
                    # Case 1: resource already exists — Azure ID in error text
                    id_match = re.search(r'"/subscriptions/[^"]+?"', detail)
                    if id_match and re.search(r'already exists', detail, re.IGNORECASE):
                        azure_id = id_match.group(0).strip('"')
                        already_exists.append((diag_addr, azure_id))
                        log("WARN", f"  will auto-import {diag_addr}")
                    # Case 2: role assignment 409 — look up full resource ID
                    elif re.search(r'RoleAssignment(Exists|Already)', detail):
                        guid_match = re.search(r'\b([0-9a-f]{32})\b', detail)
                        if guid_match:
                            full_id = _lookup_role_assignment_id(guid_match.group(1))
                            if full_id:
                                already_exists.append((diag_addr, full_id))
                                log("WARN", f"  will auto-import {diag_addr}")
                    # Case 3: azuread federated identity credential 409 — look up via az CLI
                    elif re.search(r'FederatedIdentityCredential', detail) and re.search(r'already exists', detail, re.IGNORECASE):
                        full_id = _lookup_federated_credential_import_id(diag_addr, cwd)
                        if full_id:
                            already_exists.append((diag_addr, full_id))
                            log("WARN", f"  will auto-import {diag_addr}")
                    # Case 4: AKS temporary pool 400 — VM size not available for rotation.
                    # The fix is already in Terraform code (only_critical_addons_enabled in
                    # ignore_changes), so a re-plan will produce a no-op for the AKS cluster.
                    # Returning an empty already_exists lets the caller's SystemExit handler
                    # catch this and re-plan, at which point the diff disappears.
                    elif re.search(r'temporary Agent Pool|temporary_name_for_rotation', detail) and re.search(r'400|Bad Request|not allowed', detail):
                        log("WARN", f"  AKS temporary pool creation failed (VM size unavailable) — "
                                     "will re-plan; the ignore_changes fix will suppress this diff.")
            elif level == "warning":
                print(f"  {yellow(detail[:200])}")

    proc.wait()

    if created or updated or destroyed:
        print(f"\n  {bold(cyan('─── Resources changed ─────────────────────────────'))}")
        for a in created:   print(f"  {green('+')}  {a}")
        for a in updated:   print(f"  {yellow('~')}  {a}")
        for a in destroyed: print(f"  {red('-')}  {a}")
        print()

    if proc.returncode != 0:
        if already_exists:
            return already_exists   # caller imports and retries
        log("ERR", f"terraform apply failed (exit {proc.returncode})")
        sys.exit(1)

    return []


def wait_for(fn, description: str, timeout: int = 300, interval: int = 10):
    log("INFO", f"Waiting: {description}  (timeout {timeout}s)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                log("OK", f"Ready: {description}")
                return
        except Exception as e:
            log("WARN", f"Check raised: {e}")
        remaining = int(deadline - time.time())
        print(f"  {dim(f'  … {remaining}s left')}", end="\r", flush=True)
        time.sleep(interval)
    print()
    die(f"Timeout waiting for: {description}")

# ── state ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed": [], "data": {}}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def mark_done(state: dict, n: int):
    if n not in state["completed"]:
        state["completed"].append(n)
    save_state(state)

def should_run(state: dict, n: int, start: int) -> bool:
    if n < start:
        log("INFO", dim(f"Phase {n} — skipped (--from {start})"))
        return False
    if n in state["completed"]:
        log("OK", dim(f"Phase {n} — already completed, skipping"))
        return False
    return True

# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 1 — Pre-flight checks
# ═════════════════════════════════════════════════════════════════════════════

def _current_ip() -> str:
    """Best-effort public IP detection — used to pre-fill deployer_ips / authorized_ip_ranges."""
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


def _ensure_tfvars(state: dict):
    """Create terraform/terraform.tfvars from the example if it doesn't exist yet."""
    tfvars     = REPO_ROOT / "terraform" / "terraform.tfvars"
    tfvars_ex  = REPO_ROOT / "terraform" / "terraform.tfvars.example"
    if tfvars.exists():
        return
    if not tfvars_ex.exists():
        log("WARN", "terraform.tfvars.example not found — skipping tfvars creation")
        return

    content = tfvars_ex.read_text(encoding="utf-8")

    # Substitute known values
    org  = state["data"].get("github_org",  "")
    repo = state["data"].get("github_repo", "peerisland")
    ip   = _current_ip()

    if org:
        content = content.replace('"your-github-org"', f'"{org}"')
    if repo:
        content = content.replace('"peerisland"', f'"{repo}"')
    if ip:
        content = content.replace('"1.2.3.4/32",   # GitHub Actions runner egress', f'"{ip}/32",   # your workstation')
        content = content.replace('"1.2.3.4",   # CI/CD runner egress IP', f'"{ip}",   # your workstation')
        # remove the VPN placeholder line
        content = content.replace('  "5.6.7.8/32",   # VPN gateway\n', '')

    tfvars.write_text(content, encoding="utf-8")
    log("OK",   f"Created terraform/terraform.tfvars (github_org={org or '?'}, ip={ip or '?'})")
    log("INFO", f"Review {tfvars} before continuing — edit node sizes, tags, etc. if needed")
    input(f"  {yellow('Press Enter once you have reviewed terraform.tfvars…')}")


# ── tool auto-installer ───────────────────────────────────────────────────────

_IS_WIN = sys.platform == "win32"
_TOOLS_DIR = Path(os.environ.get("LOCALAPPDATA", "C:/Users/Public")) / "Programs" / "peerisland-tools"

# Pinned versions — update here to upgrade
_VERSIONS = {
    "terraform":             "1.9.8",
    "argocd":                "v2.12.0",
    "kustomize":             "v5.4.3",
    "kubectl":               "",       # fetched dynamically from dl.k8s.io/release/stable.txt
    "kubectl-argo-rollouts": "v1.7.2",
}


def _fetch(url: str, dest: Path):
    log("INFO", f"Downloading {url.split('/')[-1]} …")
    urllib.request.urlretrieve(url, dest)


def _add_path_win(d: Path):
    s = str(d)
    if s.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = s + os.pathsep + os.environ["PATH"]
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS)
        cur, _ = winreg.QueryValueEx(key, "PATH")
        if s.lower() not in cur.lower():
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, f"{s};{cur}")
        winreg.CloseKey(key)
    except Exception:
        pass


def _kubectl_version() -> str:
    try:
        with urllib.request.urlopen("https://dl.k8s.io/release/stable.txt", timeout=10) as r:
            return r.read().decode().strip()
    except Exception:
        return "v1.29.9"


def _install_win(tool: str) -> bool:
    _TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    _add_path_win(_TOOLS_DIR)
    try:
        if tool == "terraform":
            v = _VERSIONS["terraform"]
            url = f"https://releases.hashicorp.com/terraform/{v}/terraform_{v}_windows_amd64.zip"
            with tempfile.TemporaryDirectory() as tmp:
                z = Path(tmp) / "tf.zip"
                _fetch(url, z)
                with zipfile.ZipFile(z) as zf:
                    zf.extract("terraform.exe", _TOOLS_DIR)
            log("OK", f"terraform {v} → {_TOOLS_DIR}")

        elif tool == "az":
            # pip install azure-cli works without admin and uses the already-installed Python
            py = sys.executable
            subprocess.run([py, "-m", "pip", "install", "--quiet", "azure-cli"], check=True)
            # az lands in the Scripts folder next to python.exe
            scripts = Path(py).parent / "Scripts"
            if scripts.exists():
                _add_path_win(scripts)
            log("OK", "Azure CLI installed via pip")

        elif tool == "kubectl":
            v = _kubectl_version()
            url = f"https://dl.k8s.io/release/{v}/bin/windows/amd64/kubectl.exe"
            _fetch(url, _TOOLS_DIR / "kubectl.exe")
            log("OK", f"kubectl {v} → {_TOOLS_DIR}")

        elif tool == "helm":
            v = "v3.16.3"
            url = f"https://get.helm.sh/helm-{v}-windows-amd64.zip"
            with tempfile.TemporaryDirectory() as tmp:
                z = Path(tmp) / "helm.zip"
                _fetch(url, z)
                with zipfile.ZipFile(z) as zf:
                    zf.extract("windows-amd64/helm.exe", tmp)
                Path(tmp, "windows-amd64", "helm.exe").rename(_TOOLS_DIR / "helm.exe")
            log("OK", f"helm {v} → {_TOOLS_DIR}")

        elif tool == "kubelogin":
            # Download latest kubelogin release for Windows
            req = urllib.request.Request(
                "https://api.github.com/repos/Azure/kubelogin/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "bootstrap.py"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                rel = json.loads(r.read())
            kl_ver = rel["tag_name"]
            kl_url = next(
                a["browser_download_url"] for a in rel["assets"]
                if "windows-amd64.zip" in a["name"]
            )
            with tempfile.TemporaryDirectory() as tmp:
                z = Path(tmp) / "kubelogin.zip"
                _fetch(kl_url, z)
                with zipfile.ZipFile(z) as zf:
                    # asset contains bin/windows_amd64/kubelogin.exe
                    exe_name = next(n for n in zf.namelist() if n.endswith("kubelogin.exe"))
                    zf.extract(exe_name, tmp)
                    Path(tmp, exe_name).rename(_TOOLS_DIR / "kubelogin.exe")
            log("OK", f"kubelogin {kl_ver} → {_TOOLS_DIR}")

        elif tool == "argocd":
            v = _VERSIONS["argocd"]
            url = f"https://github.com/argoproj/argo-cd/releases/download/{v}/argocd-windows-amd64.exe"
            _fetch(url, _TOOLS_DIR / "argocd.exe")
            log("OK", f"argocd {v} → {_TOOLS_DIR}")

        elif tool == "kubectl-argo-rollouts":
            v = _VERSIONS["kubectl-argo-rollouts"]
            url = (f"https://github.com/argoproj/argo-rollouts/releases/download/{v}/"
                   f"kubectl-argo-rollouts-windows-amd64")
            _fetch(url, _TOOLS_DIR / "kubectl-argo-rollouts.exe")
            log("OK", f"kubectl-argo-rollouts {v} → {_TOOLS_DIR}")

        elif tool == "kustomize":
            # kustomize mono-repo has many sub-module releases; page through until we find
            # a kustomize/vX.Y.Z release that has a windows_amd64 .tar.gz asset.
            asset_url = ""
            v = ""
            for page in range(1, 6):
                try:
                    req = urllib.request.Request(
                        f"https://api.github.com/repos/kubernetes-sigs/kustomize/releases"
                        f"?per_page=30&page={page}",
                        headers={"Accept": "application/vnd.github+json", "User-Agent": "bootstrap.py"},
                    )
                    with urllib.request.urlopen(req, timeout=15) as r:
                        rels = json.loads(r.read())
                    if not rels:
                        break
                    for rel in rels:
                        if not rel["tag_name"].startswith("kustomize/"):
                            continue
                        for asset in rel.get("assets", []):
                            if "windows_amd64" in asset["name"] and asset["name"].endswith(".tar.gz"):
                                asset_url = asset["browser_download_url"]
                                v = rel["tag_name"].replace("kustomize/", "")
                                break
                        if asset_url:
                            break
                except Exception as api_err:
                    log("WARN", f"GitHub API page {page}: {api_err}")
                if asset_url:
                    break

            if not asset_url:
                # Known-good fallback: v5.4.1 has verified Windows assets
                v = "v5.4.1"
                asset_url = (
                    f"https://github.com/kubernetes-sigs/kustomize/releases/download/"
                    f"kustomize%2F{v}/kustomize_{v}_windows_amd64.tar.gz"
                )
                log("WARN", f"API gave no asset URL — using fallback {v}")
            else:
                log("INFO", f"Found kustomize {v} via GitHub API")

            with tempfile.TemporaryDirectory() as tmp:
                tgz = Path(tmp) / "kustomize.tar.gz"
                _fetch(asset_url, tgz)
                with tarfile.open(tgz) as tf:
                    names = tf.getnames()
                    exe = next((n for n in names if "kustomize" in n.lower()), names[0])
                    tf.extract(exe, _TOOLS_DIR)
                    extracted = _TOOLS_DIR / exe
                    if extracted.name != "kustomize.exe":
                        extracted.rename(_TOOLS_DIR / "kustomize.exe")
            log("OK", f"kustomize {v} → {_TOOLS_DIR}")

        else:
            return False
        return True
    except Exception as e:
        log("WARN", f"Auto-install {tool} (Windows) failed: {e}")
        return False


def _install_linux(tool: str) -> bool:
    def sh(cmd):
        return subprocess.run(cmd, shell=True, check=True)
    try:
        if tool == "terraform":
            sh("wget -qO- https://apt.releases.hashicorp.com/gpg | "
               "sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg && "
               "echo \"deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] "
               "https://apt.releases.hashicorp.com $(lsb_release -cs) main\" | "
               "sudo tee /etc/apt/sources.list.d/hashicorp.list >/dev/null && "
               "sudo apt-get update -qq && sudo apt-get install -y terraform")

        elif tool == "az":
            sh("curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash")

        elif tool == "kubectl":
            v = _kubectl_version()
            sh(f"curl -sLo /tmp/kubectl https://dl.k8s.io/release/{v}/bin/linux/amd64/kubectl && "
               "chmod +x /tmp/kubectl && sudo mv /tmp/kubectl /usr/local/bin/kubectl")

        elif tool == "helm":
            sh("curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash")

        elif tool == "kubelogin":
            sh("curl -sL https://github.com/Azure/kubelogin/releases/latest/download/kubelogin-linux-amd64.zip "
               "-o /tmp/kubelogin.zip && unzip -qo /tmp/kubelogin.zip -d /tmp/kubelogin && "
               "sudo install -m 755 /tmp/kubelogin/bin/linux_amd64/kubelogin /usr/local/bin/kubelogin")

        elif tool == "argocd":
            v = _VERSIONS["argocd"]
            sh(f"curl -sSL -o /tmp/argocd "
               f"https://github.com/argoproj/argo-cd/releases/download/{v}/argocd-linux-amd64 && "
               "chmod +x /tmp/argocd && sudo mv /tmp/argocd /usr/local/bin/argocd")

        elif tool == "kubectl-argo-rollouts":
            v = _VERSIONS["kubectl-argo-rollouts"]
            sh(f"curl -sSL -o /tmp/kubectl-argo-rollouts "
               f"https://github.com/argoproj/argo-rollouts/releases/download/{v}/"
               f"kubectl-argo-rollouts-linux-amd64 && "
               "chmod +x /tmp/kubectl-argo-rollouts && "
               "sudo mv /tmp/kubectl-argo-rollouts /usr/local/bin/kubectl-argo-rollouts")

        elif tool == "kustomize":
            sh("curl -s \"https://raw.githubusercontent.com/kubernetes-sigs/kustomize/"
               "master/hack/install_kustomize.sh\" | bash && "
               "sudo mv kustomize /usr/local/bin/kustomize")

        else:
            return False
        return True
    except Exception as e:
        log("WARN", f"Auto-install {tool} (Linux) failed: {e}")
        return False


def _auto_install(tool: str) -> bool:
    log("INFO", f"Auto-installing {tool} …")
    return _install_win(tool) if _IS_WIN else _install_linux(tool)


# Tools that can be auto-installed on demand
_AUTO_INSTALLABLE = {"terraform", "az", "kubectl", "kubelogin", "helm", "argocd", "kustomize", "kubectl-argo-rollouts"}

# Track which tools have already been verified this session to avoid re-checking
_verified_tools: set[str] = set()


_TOOL_PROBES = {
    "az":                    "az version",
    "kubectl":               "kubectl version --client",
    "kubelogin":             "kubelogin --version",
    "helm":                  "helm version",
    "argocd":                "argocd version --client",
    "kustomize":             "kustomize version",
    "terraform":             "terraform --version",
    "gh":                    "gh --version",
    "git":                   "git --version",
    "kubectl-argo-rollouts": "kubectl-argo-rollouts version",
}


def _require_tool(tool: str):
    """Ensure a tool is on PATH, auto-installing it if needed. Dies if unavailable.

    Safe to call from any phase — uses a session cache so repeated calls are free.
    """
    if tool in _verified_tools:
        return
    probe = _TOOL_PROBES.get(tool, f"{tool} --version")
    if subprocess.run(probe, shell=True, capture_output=True).returncode == 0:
        _verified_tools.add(tool)
        return
    # Not found — try to install
    if tool not in _AUTO_INSTALLABLE:
        die(f"{tool} is not installed and cannot be auto-installed — install it manually")
    log("WARN", f"{tool} not found — auto-installing …")
    if _auto_install(tool):
        _ensure_tools_on_path()
        if subprocess.run(probe, shell=True, capture_output=True).returncode == 0:
            log("OK", f"{tool} installed and ready")
            _verified_tools.add(tool)
            return
    die(f"{tool} auto-install failed or not on PATH — open a new terminal and re-run")


_REQUIRED_PROVIDERS = [
    "Microsoft.ContainerService",
    "Microsoft.ContainerRegistry",
    "Microsoft.KeyVault",
    "Microsoft.Network",
    "Microsoft.Compute",
    "Microsoft.Storage",
    "Microsoft.OperationalInsights",
    "Microsoft.OperationsManagement",
    "Microsoft.ManagedIdentity",
    "Microsoft.Authorization",
    "Microsoft.Resources",
    "Microsoft.Insights",
]


def _register_azure_providers():
    log("INFO", "Registering required Azure resource providers …")
    for ns in _REQUIRED_PROVIDERS:
        r = subprocess.run(
            f"az provider show --namespace {ns} --query registrationState -o tsv",
            shell=True, capture_output=True, text=True,
        )
        if r.stdout.strip() == "Registered":
            log("OK", f"  {ns} already registered")
            continue
        log("INFO", f"  Registering {ns} …")
        subprocess.run(f"az provider register --namespace {ns} --wait", shell=True, check=True)
        log("OK", f"  {ns} registered")


def phase1_preflight(state: dict):
    phase_header(1, "Pre-flight checks")

    # All tools needed across all phases — install everything upfront.
    # gh and git must be pre-installed (no unattended install path).
    # Everything else is auto-installed via _require_tool().
    all_tools = [
        "terraform", "az", "kubectl", "kubelogin",
        "helm", "argocd", "kustomize",
    ]
    manual_tools = ["gh", "git"]

    still_missing = []
    for tool in manual_tools:
        probe = {"gh": "gh --version", "git": "git --version"}[tool]
        if subprocess.run(probe, shell=True, capture_output=True).returncode == 0:
            log("OK", f"{tool:12} installed")
        else:
            log("ERR", f"{tool:12} NOT FOUND — install manually and re-run")
            still_missing.append(tool)

    for tool in all_tools:
        try:
            _require_tool(tool)
            log("OK", f"{tool:12} ready")
        except SystemExit:
            still_missing.append(tool)

    if still_missing:
        die(f"Could not install: {', '.join(still_missing)}")

    # Azure login
    az_ok = subprocess.run("az account show", shell=True, capture_output=True).returncode == 0
    if not az_ok:
        log("WARN", "Not logged into Azure — running 'az login'")
        run("az login")

    account = json.loads(capture("az account show"))
    log("OK", f"Azure subscription: {account['name']}  ({account['id']})")
    state["data"]["subscription_id"] = account["id"]
    state["data"]["tenant_id"]       = account["tenantId"]

    # GitHub auth
    gh_ok = subprocess.run("gh auth status", shell=True, capture_output=True).returncode == 0
    if not gh_ok:
        log("WARN", "Not logged into GitHub — running 'gh auth login'")
        run("gh auth login")

    # Git remote
    remote = capture("git remote get-url origin", check=False)
    if not remote:
        die("No git remote 'origin'. Push the repo to GitHub first.")
    log("OK", f"Git remote: {remote}")

    # Parse github_org / github_repo from the remote URL
    # Handles both HTTPS and SSH formats
    clean = remote.rstrip("/").removesuffix(".git")
    if "github.com" in clean:
        parts = clean.replace("git@github.com:", "https://github.com/").split("github.com/")[-1].split("/")
        state["data"]["github_org"]  = parts[0]
        state["data"]["github_repo"] = parts[1]
        log("OK", f"GitHub repo: {parts[0]}/{parts[1]}")

    # Register resource providers — required on new subscriptions
    _register_azure_providers()

    _ensure_tfvars(state)
    mark_done(state, 1)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 2 — Bootstrap Terraform remote state
# ═════════════════════════════════════════════════════════════════════════════

def phase2_bootstrap_tfstate(state: dict):
    phase_header(2, "Bootstrap Terraform remote state")
    tf_boot = REPO_ROOT / "terraform" / "bootstrap"

    if not tf_boot.exists():
        log("WARN", "terraform/bootstrap/ not found — skipping")
        log("INFO", "Ensure providers.tf backend block is configured manually")
        mark_done(state, 2)
        return

    run("terraform init",                                cwd=tf_boot)
    run("terraform plan -out=tfplan_boot -input=false", cwd=tf_boot)
    tf_apply("tfplan_boot", cwd=tf_boot)

    raw = capture("terraform output -json", cwd=tf_boot, check=False)
    if raw:
        outs = json.loads(raw)
        sa   = outs.get("storage_account_name", {}).get("value", "")
        rg   = outs.get("resource_group_name",  {}).get("value", "")
        if sa:
            log("OK", f"TF state backend: storage_account={sa}  rg={rg}")
            state["data"]["tf_state_sa"] = sa
            state["data"]["tf_state_rg"] = rg

            # Patch providers.tf backend block automatically
            providers_tf = REPO_ROOT / "terraform" / "providers.tf"
            pf_content = providers_tf.read_text(encoding="utf-8")
            if "REPLACE_WITH_BOOTSTRAP_OUTPUT" in pf_content:
                pf_content = pf_content.replace("REPLACE_WITH_BOOTSTRAP_OUTPUT", sa)
                providers_tf.write_text(pf_content, encoding="utf-8")
                log("OK", f"providers.tf backend patched → storage_account_name = {sa}")
            else:
                log("INFO", "providers.tf already configured — skipping patch")

    mark_done(state, 2)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 3 — Set GitHub Actions secrets
# ═════════════════════════════════════════════════════════════════════════════

def _tf_cicd_app_ids() -> tuple:
    """Return (client_id, object_id) for azuread_application.cicd from Terraform state/output.

    Uses `terraform output` first (fastest), falls back to `terraform state show`.
    Returns ('', '') if Terraform hasn't applied yet.
    """
    tf_dir = REPO_ROOT / "terraform"
    # Try outputs first
    out_raw = capture("terraform output -json", cwd=tf_dir, check=False)
    if out_raw.strip().startswith("{"):
        try:
            outs = json.loads(out_raw)
            cid = outs.get("cicd_client_id", {}).get("value", "")
            if cid:
                # object_id not in outputs — read from state
                st = capture("terraform state show azuread_application.cicd",
                             cwd=tf_dir, check=False)
                oid = ""
                for line in st.splitlines():
                    s = line.strip()
                    if s.startswith("object_id") and "=" in s:
                        oid = s.split("=", 1)[-1].strip().strip('"')
                        break
                return cid, oid
        except Exception:
            pass
    # Fall back to state show
    st = capture("terraform state show azuread_application.cicd",
                 cwd=tf_dir, check=False)
    cid = oid = ""
    for line in st.splitlines():
        s = line.strip()
        if s.startswith("client_id") and "=" in s and not cid:
            cid = s.split("=", 1)[-1].strip().strip('"')
        if s.startswith("object_id") and "=" in s and not oid:
            oid = s.split("=", 1)[-1].strip().strip('"')
    return cid, oid


def phase3_github_secrets(state: dict):
    phase_header(3, "Set GitHub Actions secrets")
    org       = state["data"].get("github_org",  "")
    repo_name = state["data"].get("github_repo", "")
    sub       = state["data"].get("subscription_id", "")
    tid       = state["data"].get("tenant_id", "")

    # SP was created by Terraform — read its client_id from outputs.
    # Phase 3 runs before Terraform (phase 4), so we try TF outputs if already applied,
    # otherwise fall back to az ad sp lookup.
    tf_dir = REPO_ROOT / "terraform"
    tf_outs_raw = capture("terraform output -json", cwd=tf_dir, check=False)
    tf_outs = json.loads(tf_outs_raw) if tf_outs_raw.startswith("{") else {}

    def tf(key: str) -> str:
        return tf_outs.get(key, {}).get("value", "") or ""

    client_id, _ = _tf_cicd_app_ids()
    client_id = client_id or tf("cicd_client_id")

    tenant_id_val = tf("cicd_tenant_id") or tid
    sub_id_val    = tf("cicd_subscription_id") or sub

    if client_id:
        log("OK", f"SP client ID from Terraform: {client_id}")
    else:
        log("WARN", "SP not found — Terraform hasn't run yet. Run phase 4 first, then re-run phase 3.")
        log("WARN", "Continuing with empty AZURE_CLIENT_ID; update it after terraform apply.")

    # ACR / AKS values may not be known yet — set placeholders; phase 4 overwrites them
    acr_name   = state["data"].get("acr_name",          "peerislandprodacr")
    acr_server = state["data"].get("acr_login_server",  "peerislandprodacr.azurecr.io")
    aks_name   = state["data"].get("aks_cluster_name",  "peerisland-prod-aks")
    aks_rg     = state["data"].get("aks_resource_group","peerisland-prod-rg")

    def set_secret(name: str, val: str):
        if not val:
            log("WARN", f"Skipped {name} (empty — will be set after terraform apply)")
            return
        run(f'gh secret set {name} --repo {org}/{repo_name} --body "{val}"')
        log("OK", f"Set {name}")

    for name, val in [
        ("AZURE_CLIENT_ID",       client_id),
        ("AZURE_TENANT_ID",       tenant_id_val),
        ("AZURE_SUBSCRIPTION_ID", sub_id_val),
        ("ACR_NAME",              acr_name),
        ("ACR_LOGIN_SERVER",      acr_server),
        ("AKS_CLUSTER_NAME",      aks_name),
        ("AKS_RESOURCE_GROUP",    aks_rg),
    ]:
        set_secret(name, val)

    state["data"].update({
        "acr_name": acr_name, "acr_login_server": acr_server,
        "aks_cluster_name": aks_name, "aks_resource_group": aks_rg,
    })
    mark_done(state, 3)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 4 — Provision Azure infra (Terraform)
# ═════════════════════════════════════════════════════════════════════════════

def _tf_stream(cmd: str, cwd: str):
    """Run a terraform command, stream output live, return (returncode, combined_output)."""
    proc = subprocess.Popen(
        cmd, shell=True, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    lines = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    proc.wait()
    return proc.returncode, "".join(lines)


def _tf_extract_lock_id(output: str) -> str:
    for line in output.splitlines():
        if "ID:" in line:
            return line.split("ID:")[-1].strip()
    return ""


def _terraform_drift_apply(state: dict):
    """Run terraform plan on every bootstrap re-run; auto-apply if drift detected (no prompt)."""
    tf_dir = REPO_ROOT / "terraform"
    if not (tf_dir / "terraform.tfvars").exists():
        return
    log("INFO", "Checking terraform for infrastructure drift …")
    # -lock=false is safe for plan (read-only); avoids failing on a stale lock from a prior run
    r = subprocess.run(
        "terraform plan -detailed-exitcode -lock=false -out=tfplan",
        shell=True, cwd=str(tf_dir),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode == 0:
        log("OK", "Terraform: no changes — infrastructure up to date")
        return
    if r.returncode == 1:
        err = r.stderr or r.stdout or ""
        if "state lock" in err.lower() or "already locked" in err.lower():
            lock_id = _tf_extract_lock_id(err)
            if lock_id:
                log("WARN", f"Stale state lock (ID: {lock_id}) — force-unlocking …")
                subprocess.run(f"terraform force-unlock -force {lock_id}",
                               shell=True, cwd=str(tf_dir), capture_output=True)
                r = subprocess.run(
                    "terraform plan -detailed-exitcode -out=tfplan",
                    shell=True, cwd=str(tf_dir),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                if r.returncode == 1:
                    log("WARN", f"Terraform plan failed after unlock: {(r.stderr or r.stdout)[:200]}")
                    return
            else:
                log("WARN", f"Terraform plan failed (lock — no ID found): {err[:200]}")
                return
        else:
            log("WARN", f"Terraform plan failed: {err[:200]}")
            return
    if r.returncode == 0:
        log("OK", "Terraform: no changes — infrastructure up to date")
        return

    # exit code 2 = changes pending — show what will change
    log("WARN", "Terraform drift detected — changes to apply:")
    for line in (r.stdout or "").splitlines():
        # print resource-change lines and the Plan summary line
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("Plan:") or stripped.startswith("~") or stripped.startswith("+") or stripped.startswith("-"):
            print(f"  {line}")
    print()

    log("WARN", "Auto-applying terraform drift …")
    rc, out = _tf_stream("terraform apply -auto-approve tfplan", str(tf_dir))
    if rc != 0:
        if "state blob is already locked" in out.lower() or "error acquiring the state lock" in out.lower():
            lock_id = _tf_extract_lock_id(out)
            if lock_id:
                log("WARN", f"Stale lock on apply (ID: {lock_id}) — force-unlocking and retrying …")
                subprocess.run(f"terraform force-unlock -force {lock_id}",
                               shell=True, cwd=str(tf_dir), capture_output=True)
                rc2, _ = _tf_stream("terraform apply -auto-approve tfplan", str(tf_dir))
                if rc2 != 0:
                    log("WARN", "Terraform auto-apply failed after unlock — continuing (manual apply may be needed)")
                else:
                    log("OK", "Terraform drift resolved")
                return
        log("WARN", "Terraform auto-apply failed — continuing (manual apply may be needed)")
    else:
        log("OK", "Terraform drift resolved")


def phase4_terraform(state: dict):
    phase_header(4, "Provision Azure infrastructure (Terraform)")
    tf_dir = REPO_ROOT / "terraform"

    run("terraform init -upgrade", cwd=tf_dir)
    run("terraform validate",      cwd=tf_dir)

    # ── Pre-flight imports ────────────────────────────────────────────────────
    # Adopt resources that exist in Azure/Helm but are not yet in Terraform state.
    # This prevents 409 Conflict errors during apply.

    def _in_state(addr: str) -> bool:
        r = subprocess.run(f'terraform state show "{addr}"',
                           shell=True, capture_output=True, cwd=str(tf_dir),
                           env=os.environ.copy())
        return r.returncode == 0

    # ArgoCD Helm release (installed by bootstrap phase 7, now managed by Terraform)
    if not _in_state("helm_release.argocd"):
        helm_check = subprocess.run(
            "helm status argocd -n argocd -o json",
            shell=True, capture_output=True, text=True, env=os.environ.copy(),
        )
        if helm_check.returncode == 0:
            log("INFO", "Importing existing ArgoCD Helm release into Terraform state …")
            run('terraform import helm_release.argocd "argocd/argocd"', cwd=tf_dir, check=False)

    # Federated identity credential (may exist from a previous non-Terraform apply)
    if not _in_state("azuread_application_federated_identity_credential.github_main"):
        import_id = _lookup_federated_credential_import_id(
            "azuread_application_federated_identity_credential.github_main", tf_dir
        )
        if import_id:
            log("INFO", "Importing existing federated identity credential into Terraform state …")
            run(f'terraform import "azuread_application_federated_identity_credential.github_main" "{import_id}"',
                cwd=tf_dir, check=False)

    MAX_ATTEMPTS = 5
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log("INFO", f"Planning infrastructure (attempt {attempt}/{MAX_ATTEMPTS}) …")
        run("terraform plan -out=tfplan", cwd=tf_dir)

        if attempt == 1:
            confirm = input(f"\n  {yellow('Apply the plan above? [y/N]: ')}").strip().lower()
            if confirm != "y":
                die("Terraform apply cancelled")
        else:
            log("INFO", f"Auto-retrying after partial failure (attempt {attempt}/{MAX_ATTEMPTS}) …")

        try:
            to_import = tf_apply("tfplan", cwd=tf_dir)
            if to_import:
                # Resources exist in Azure but not in state — import them, then retry
                log("WARN", f"Found {len(to_import)} resource(s) that exist in Azure but not in state — importing …")
                for tf_addr, azure_id in to_import:
                    log("INFO", f"  terraform import {tf_addr}")
                    run(f'terraform import "{tf_addr}" "{azure_id}"', cwd=tf_dir, check=False)
                if attempt == MAX_ATTEMPTS:
                    die("Terraform apply failed after 3 attempts — check errors above")
                log("INFO", "Import done — re-planning …")
                time.sleep(3)
                continue
            log("OK", "Terraform apply complete.")
            break  # success
        except SystemExit:
            if attempt == MAX_ATTEMPTS:
                die("Terraform apply failed after 3 attempts — check errors above")
            log("WARN", f"Apply failed on attempt {attempt} — re-planning before retry …")
            time.sleep(5)

    # Parse outputs
    outs = json.loads(capture("terraform output -json", cwd=tf_dir))

    def tf(key: str) -> str:
        return outs.get(key, {}).get("value", "") or ""

    rg      = tf("resource_group_name")
    cluster = tf("aks_cluster_name")   or state["data"].get("aks_cluster_name", "peerisland-prod-aks")
    acr_srv = tf("acr_login_server")   or state["data"].get("acr_login_server", "")
    acr_nm  = tf("acr_name")           or state["data"].get("acr_name", "")
    kv_uri  = tf("key_vault_uri")      or ""
    # Key Vault name: strip "https://" and ".vault.azure.net/"
    kv_name = kv_uri.replace("https://", "").rstrip("/").replace(".vault.azure.net", "") if kv_uri else state["data"].get("kv_name", "")

    state["data"].update({
        "aks_resource_group": rg,
        "aks_cluster_name":   cluster,
        "acr_login_server":   acr_srv,
        "acr_name":           acr_nm,
        "kv_name":            kv_name,
        "kv_uri":             kv_uri,
    })

    log("OK", f"AKS:       {cluster}  (rg: {rg})")
    log("OK", f"ACR:       {acr_srv}")
    log("OK", f"Key Vault: {kv_name}")

    # Resolve App Gateway public IP from Azure
    log("INFO", "Resolving App Gateway public IP…")
    _appgw_lines = capture(
        f"az network public-ip list -g {rg} --query \"[?contains(name,'appgw') || contains(name,'appgateway')].ipAddress\" -o tsv",
        check=False,
    ).strip().splitlines() if not DRY_RUN else ["0.0.0.0"]
    appgw_ip = _appgw_lines[0] if _appgw_lines else ""
    if appgw_ip:
        log("OK", f"App Gateway IP: {appgw_ip}")
        state["data"]["appgw_ip"] = appgw_ip
    else:
        log("WARN", "Could not auto-detect App Gateway IP — will try again after kubectl is configured")

    # Update GitHub secrets with Terraform-confirmed values
    org  = state["data"].get("github_org",  "")
    repo_name = state["data"].get("github_repo", "")
    for name, val in [
        ("ACR_NAME",          acr_nm),
        ("ACR_LOGIN_SERVER",  acr_srv),
        ("AKS_CLUSTER_NAME",  cluster),
        ("AKS_RESOURCE_GROUP", rg),
    ]:
        if val and org and repo_name:
            run(f'gh secret set {name} --repo {org}/{repo_name} --body "{val}"')

    mark_done(state, 4)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 5 — Configure kubectl
# ═════════════════════════════════════════════════════════════════════════════

def phase5_kubectl(state: dict):
    phase_header(5, "Configure kubectl")
    for t in ("kubectl", "kubelogin"):
        _require_tool(t)
    cluster = state["data"].get("aks_cluster_name",  "peerisland-prod-aks")
    rg      = state["data"].get("aks_resource_group", "peerisland-prod-rg")

    # Ensure kubelogin is available (AAD-enabled clusters require it)
    if subprocess.run("kubelogin --version", shell=True, capture_output=True).returncode != 0:
        log("INFO", "kubelogin not found — installing via az aks install-cli …")
        run("az aks install-cli")
        # Rescan PATH — az aks install-cli added dirs to registry but not this process
        _ensure_tools_on_path()

    run(f"az aks get-credentials -g {rg} -n {cluster} --overwrite-existing")
    # AAD-enabled clusters require kubelogin to exchange az CLI tokens
    run("kubelogin convert-kubeconfig -l azurecli")

    nodes_json = capture("kubectl get nodes -o json")
    items = json.loads(nodes_json).get("items", [])
    for n in items:
        name   = n["metadata"]["name"]
        conds  = n["status"]["conditions"]
        status = next((c["type"] for c in reversed(conds) if c["status"] == "True"), "Unknown")
        log("OK", f"  node {name} → {status}")
    log("OK", f"{len(items)} node(s) ready")

    # Resolve ingress IP — try NGINX LB first, fall back to AppGW
    if not state["data"].get("appgw_ip"):
        nginx_ip = capture(
            "kubectl get svc ingress-nginx-controller -n ingress-nginx "
            "-o jsonpath='{.status.loadBalancer.ingress[0].ip}'",
            check=False,
        ).strip()
        if nginx_ip:
            state["data"]["appgw_ip"] = nginx_ip
            log("OK", f"Ingress IP (NGINX): {nginx_ip}")
        else:
            log("INFO", "NGINX LB IP not yet assigned — will resolve after ArgoCD sync")

    mark_done(state, 5)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 6 — Seed Key Vault secrets
# ═════════════════════════════════════════════════════════════════════════════

def phase6_keyvault(state: dict):
    phase_header(6, "Seed Key Vault secrets")
    kv = state["data"].get("kv_name", "")

    print(f"\n  {yellow('Enter values for Key Vault secrets.')}")
    print(f"  {dim('Secrets already present in Key Vault are skipped automatically.')}\n")

    # Check which secrets are already in Key Vault
    _existing_kv = set(capture(
        f'az keyvault secret list --vault-name {kv} --query "[].name" -o tsv',
        check=False,
    ).splitlines())

    # The 5 real secrets this platform needs
    secrets_spec = [
        ("grafana-admin-user",    "Grafana admin username (e.g. admin)",          False),
        ("grafana-admin-password","Grafana admin password (min 24 chars)",         True),
        ("grafana-slack-webhook", "Slack incoming webhook URL",                    True),
        ("argocd-github-token",   "GitHub PAT with repo read access (for ArgoCD)", True),
        ("tls-certificate",       "Path to PEM cert file, or leave blank for cert-manager", False),
        ("tls-private-key",       "Path to PEM key file, or leave blank for cert-manager",  False),
    ]

    for name, prompt, sensitive in secrets_spec:
        if name in _existing_kv:
            log("OK", f"{name} already in Key Vault — skipping")
            continue
        # Resolution order:
        #   1. env var override      (ARGOCD_GITHUB_TOKEN etc.)
        #   2. gh auth token         (argocd-github-token only — reuse authenticated session)
        #   3. interactive prompt    (last resort)
        env_key = name.upper().replace("-", "_")
        val = os.environ.get(env_key, "").strip()
        if val:
            log("INFO", f"{name}: using env var {env_key}")
        elif name == "argocd-github-token":
            val = capture("gh auth token", check=False).strip()
            if val:
                log("INFO", "argocd-github-token: using gh CLI session token (has repo scope)")
            else:
                log("WARN", "gh auth token returned nothing — run 'gh auth login' first")
        if not val:
            fn  = getpass.getpass if sensitive else input
            val = fn(f"  {cyan(name)} — {dim(prompt)}: ").strip()
        if not val:
            log("INFO", f"Skipped {name}")
            continue

        if name in ("tls-certificate", "tls-private-key"):
            path = Path(val)
            if path.exists():
                run(f'az keyvault secret set --vault-name {kv} --name {name} --file "{path}"')
            else:
                log("WARN", f"File not found: {val} — skipping {name}")
        else:
            # Escape quotes in value before embedding in shell string
            escaped = val.replace('"', '\\"')
            run(f'az keyvault secret set --vault-name {kv} --name {name} --value "{escaped}"')
            if name == "grafana-admin-password":
                state["data"]["grafana_password"] = val
            if name == "grafana-admin-user":
                state["data"]["grafana_user"] = val

    # Verify by listing
    listed = capture(
        f'az keyvault secret list --vault-name {kv} --query "[].name" -o tsv',
        check=False,
    )
    log("OK", f"Secrets now in Key Vault: {', '.join(listed.splitlines())}")
    mark_done(state, 6)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 7 — Install ArgoCD + App of Apps
# ═════════════════════════════════════════════════════════════════════════════

def phase7_argocd(state: dict):
    phase_header(7, "Install ArgoCD + App of Apps")
    for t in ("kubectl", "helm", "argocd"):
        _require_tool(t)
    org       = state["data"].get("github_org",  "")
    repo_name = state["data"].get("github_repo", "")

    # Patch placeholder org/repo in ArgoCD YAML files
    if org and repo_name:
        for yaml_path in (REPO_ROOT / "argocd").rglob("*.yaml"):
            text = yaml_path.read_text(encoding="utf-8", errors="replace")
            updated = False
            for placeholder in ("<YOUR_ORG>", "YOUR_ORG", "your-org"):
                if placeholder in text:
                    text    = text.replace(placeholder, org)
                    updated = True
            for placeholder in ("<YOUR_REPO>", "YOUR_REPO", "your-repo", "peerisland"):
                # Only replace standalone placeholders, not the actual project name in paths
                pass  # repo name is already 'peerisland' in this project
            if updated:
                yaml_path.write_text(text, encoding="utf-8")
                log("OK", f"Patched {yaml_path.relative_to(REPO_ROOT)}")

    # Install ArgoCD via Helm (matches install-argocd.sh but cross-platform)
    argocd_values = REPO_ROOT / "argocd" / "argocd-values.yaml"
    run("kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -")
    run("helm repo add argo https://argoproj.github.io/argo-helm", check=False)
    run("helm repo update")
    helm_cmd = (
        "helm upgrade --install argocd argo/argo-cd "
        "--namespace argocd "
        "--version 6.7.3 "
        "--wait --timeout 10m"
    )
    if argocd_values.exists():
        helm_cmd += f' --values "{argocd_values}"'
    run(helm_cmd)

    # Wait for argocd-server deployment
    def argocd_ready():
        r = subprocess.run(
            "kubectl get deploy argocd-server -n argocd -o jsonpath='{.status.readyReplicas}'",
            shell=True, capture_output=True, text=True,
        )
        return r.stdout.strip() not in ("", "0", "<no value>")

    wait_for(argocd_ready, "ArgoCD server deployment ready", timeout=300, interval=10)

    # Retrieve initial admin password
    pw_b64 = capture(
        "kubectl -n argocd get secret argocd-initial-admin-secret "
        "-o jsonpath='{.data.password}'",
        check=False,
    )
    if pw_b64:
        # base64.b64decode tolerates missing padding
        argocd_pw = base64.b64decode(pw_b64 + "==").decode().strip()
        state["data"]["argocd_password"] = argocd_pw
        log("OK", "ArgoCD initial admin password retrieved")
        sep = "─" * 56
        print(f"\n  {green('ArgoCD is ready')}")
        print(f"  {sep}")
        print(f"  URL       https://<nginx-lb-ip>  (IP available after Phase 8)")
        print(f"  Username  admin")
        print(f"  Password  {argocd_pw}")
        print(f"  {sep}\n")

    # Register GitHub repo credential with ArgoCD using the PAT from Key Vault
    kv = state["data"].get("kv_name", "")
    if kv:
        gh_token = capture(
            f"az keyvault secret show --vault-name {kv} --name argocd-github-token "
            f"--query value -o tsv",
            check=False,
        ).strip()
        if gh_token and org and repo_name:
            repo_url = f"https://github.com/{org}/{repo_name}"
            run(
                f"argocd repo add {repo_url} "
                f"--username git --password {gh_token} --upsert",
                check=False,
            )
            log("OK", f"ArgoCD repo credential registered for {repo_url}")
        else:
            log("WARN", "argocd-github-token not found in Key Vault — repo may fail to sync")

    # ── Grafana admin secret ─────────────────────────────────────────────────
    # grafana-values.yaml uses existingSecret: grafana-admin-secret — create it
    # if missing, pulling from Key Vault or generating a fresh one.
    kv_name = state["data"].get("key_vault_name", "")
    grafana_pass = ""
    if kv_name:
        kv_r = subprocess.run(
            f"az keyvault secret show --vault-name {kv_name} "
            f"--name grafana-admin-password --query value -o tsv",
            shell=True, capture_output=True, text=True, encoding="utf-8",
        )
        if kv_r.returncode == 0:
            grafana_pass = kv_r.stdout.strip()
    if not grafana_pass:
        import secrets as _sec, string as _str
        grafana_pass = "".join(
            _sec.choice(_str.ascii_letters + _str.digits + "!@#$%")
            for _ in range(16)
        )
        log("INFO", f"Generated Grafana admin password (admin / {grafana_pass})")
        if kv_name:
            subprocess.run(
                f"az keyvault secret set --vault-name {kv_name} "
                f"--name grafana-admin-password --value {grafana_pass!r}",
                shell=True, capture_output=True, encoding="utf-8",
            )

    run("kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -")
    run(
        f"kubectl create secret generic grafana-admin-secret -n monitoring "
        f"--from-literal=admin-user=admin "
        f"--from-literal=admin-password={grafana_pass} "
        f"--dry-run=client -o yaml | kubectl apply -f -"
    )
    log("OK", f"grafana-admin-secret ready  →  username: admin  password: {grafana_pass}")
    state["data"]["grafana_admin_password"] = grafana_pass

    # Patch argocd-cm so kustomize overlays can reference parent directories
    # (k8s/overlays/production → k8s/ traverses an ancestor without this flag)
    run(
        "kubectl patch configmap argocd-cm -n argocd --type=merge "
        '--patch \'{"data":{"kustomize.buildOptions":"--load-restrictor LoadRestrictionsNone"}}\'',
        check=False,
    )
    run("kubectl rollout restart deployment argocd-repo-server -n argocd", check=False)
    run(
        "kubectl rollout status deployment argocd-repo-server -n argocd --timeout=120s",
        check=False,
    )
    run(
        "kubectl rollout restart statefulset argocd-application-controller -n argocd",
        check=False,
    )
    run(
        "kubectl rollout status statefulset argocd-application-controller -n argocd --timeout=180s",
        check=False,
    )
    log("OK", "argocd-cm patched — repo-server + app-controller restarted")

    # Apply App of Apps
    run("kubectl apply -f argocd/app-of-apps.yaml")
    log("OK", "App of Apps applied — sync waves starting")

    mark_done(state, 7)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 8 — Wait for ArgoCD sync waves
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_vcpu_quota(state: dict, vcpus_needed: int = 8):
    """Request vCPU quota increase for the VM family used by this cluster if quota is insufficient."""
    import json as _json, re as _re, tempfile as _tmp, os as _os, time as _time

    sub_id   = state["data"].get("subscription_id", "")
    location = state["data"].get("location", "centralindia")
    vm_size  = state["data"].get("node_pool_vm_size", "Standard_D4s_v5")

    if not sub_id:
        sub_r = subprocess.run("az account show --query id -o tsv", shell=True,
                               capture_output=True, text=True, encoding="utf-8")
        sub_id = sub_r.stdout.strip()

    # Derive SKU family name: Standard_D4s_v5 → standardDSv5Family
    m = _re.match(r"Standard_([A-Z]+)(\d+)([a-z]*)(?:_(v\d+))?", vm_size)
    if not m:
        log("WARN", f"Cannot parse VM family from {vm_size} — skipping quota check")
        return
    series = m.group(1)          # D
    suffix = m.group(3).upper()  # s → S
    ver    = m.group(4) or ""    # v5 (keep lowercase as Azure expects)
    family = f"standard{series}{suffix}{ver}Family"  # standardDSv5Family

    # ── Check current usage — fetch all items, filter in Python ──────────────
    cur = subprocess.run(
        f"az vm list-usage --location {location} -o json",
        shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if cur.returncode == 0:
        try:
            rows = _json.loads(cur.stdout)
            # find by exact value OR fuzzy match on localizedValue
            matched = next(
                (r for r in rows
                 if r.get("name", {}).get("value", "").lower() == family.lower()
                 or family.lower() in r.get("name", {}).get("localizedValue", "").lower().replace(" ", "")),
                None,
            )
            if matched:
                used  = matched.get("currentValue", 0)
                limit = matched.get("limit", 0)
                avail = int(limit) - int(used)
                actual_family = matched["name"]["value"]
                family = actual_family  # use whatever Azure calls it
                log("INFO", f"vCPU quota [{family}]: limit={limit}, used={used}, available={avail}")
                if avail >= vcpus_needed:
                    log("OK", f"Sufficient vCPU quota ({avail} free, need {vcpus_needed})")
                    return
        except Exception:
            pass

    # ── Request increase via az quota extension ───────────────────────────────
    new_limit = max(vcpus_needed * 4, 16)
    log("WARN", f"Requesting vCPU quota increase for {family} → {new_limit} in {location} …")

    # Ensure quota extension is installed and Microsoft.Quota provider is registered
    subprocess.run("az extension add --name quota --yes", shell=True,
                   capture_output=True, encoding="utf-8")
    reg = subprocess.run(
        "az provider show --namespace Microsoft.Quota --query registrationState -o tsv",
        shell=True, capture_output=True, text=True, encoding="utf-8",
    )
    if reg.stdout.strip().lower() != "registered":
        log("INFO", "Registering Microsoft.Quota provider (one-time, ~30 s) …")
        subprocess.run("az provider register --namespace Microsoft.Quota --wait",
                       shell=True, encoding="utf-8")

    scope = f"/subscriptions/{sub_id}/providers/Microsoft.Compute/locations/{location}"
    r = subprocess.run(
        f"az quota update --resource-name {family} --scope \"{scope}\" "
        f"--limit-object value={new_limit} -o json",
        shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )

    if r.returncode != 0:
        log("WARN", f"Quota auto-request failed: {(r.stderr or r.stdout)[:200]}")
        log("WARN",
            f"Request manually: https://portal.azure.com/#view/Microsoft_Azure_Capacity/"
            f"QuotaMenuBlade/~/overview  "
            f"(filter: location={location}, search: DSv5, request ≥{new_limit} vCPUs)")
        return

    try:
        resp   = _json.loads(r.stdout)
        status = resp.get("properties", {}).get("provisioningState", "unknown")
        log("OK", f"Quota request submitted — status: {status}")
        if status.lower() in ("succeeded", "created"):
            return
        for _ in range(12):
            _time.sleep(15)
            chk = subprocess.run(
                f"az quota show --resource-name {family} --scope \"{scope}\" -o json",
                shell=True, capture_output=True, text=True, encoding="utf-8",
            )
            if chk.returncode == 0:
                d   = _json.loads(chk.stdout)
                lim = d.get("properties", {}).get("limit", {}).get("value", 0)
                if int(lim) >= new_limit:
                    log("OK", f"Quota approved: {family} = {lim} vCPUs")
                    return
        log("WARN", "Quota pending — continuing (may still fail to provision node)")
    except Exception:
        log("OK", "Quota request submitted")


def _ensure_user_nodes(state: dict):
    """Scale user node pool to 1 if it has 0 nodes — workload pods can't schedule otherwise."""
    rg      = state["data"].get("aks_resource_group", "peerisland-prod-rg")
    cluster = state["data"].get("aks_cluster_name",  "peerisland-prod-aks")

    result = subprocess.run(
        f"az aks nodepool show -g {rg} --cluster-name {cluster} --name user "
        f"--query count -o tsv",
        shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    count = result.stdout.strip()
    if count == "0" or count == "":
        log("WARN", "User node pool has 0 nodes — bumping min-count to 1 so autoscaler provisions a node …")
        r = subprocess.run(
            f"az aks nodepool update -g {rg} --cluster-name {cluster} "
            f"--name user --update-cluster-autoscaler --min-count 1 --max-count 5",
            shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            err = (r.stderr or "") + (r.stdout or "")
            if "InsufficientVCPUQuota" in err or "vcpu quota" in err.lower():
                die(
                    "Azure vCPU quota exhausted in centralindia.\n"
                    "  → Request more quota: Azure Portal → Subscriptions → "
                    "Usage + quotas → filter 'centralindia' → "
                    "'Standard DSv5 Family vCPUs' → Request increase\n"
                    "  → Or use a smaller VM: set node_pool_vm_size = \"Standard_D2s_v3\" "
                    "in terraform/terraform.tfvars, re-run terraform apply, then bootstrap.py"
                )
            raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
        log("OK", "User node pool min-count bumped — waiting for node to be Ready …")
        # Wait up to 5 minutes for the new node
        def node_ready():
            r = subprocess.run(
                "kubectl get nodes -l agentpool=user --no-headers",
                shell=True, capture_output=True, text=True, encoding="utf-8",
            )
            return "Ready" in r.stdout
        wait_for(node_ready, "user node Ready", timeout=300, interval=15)
    else:
        log("OK", f"User node pool has {count} node(s) — OK")


def phase8_wait_argocd(state: dict):
    phase_header(8, "Wait for ArgoCD sync waves to complete")

    # Ensure kubelogin is installed and kubectl is authenticated
    # (needed when this phase is re-run from a fresh shell that skipped Phase 5)
    _ensure_kubectl_auth(state)

    # Apply resource limits + repo-server replicas=2 before the first heavy sync
    # wave starts.  This prevents repo-server OOMKills (which cause the global
    # rpc error) on clusters bootstrapped before these values were added.
    # helm upgrade is a no-op when values haven't changed, so this is safe to
    # call every time Phase 8 runs.
    _ensure_argocd_resources_patched()

    # Show current app status so the user can see what ArgoCD sees at the start
    _print_argocd_status_table("ArgoCD Apps — initial state")

    # Print access credentials so the user can log in while sync is in progress
    argocd_pw    = state["data"].get("argocd_password", "")
    grafana_pw   = state["data"].get("grafana_admin_password", "")
    if not grafana_pw:
        kv = state["data"].get("kv_name", "") or state["data"].get("key_vault_name", "")
        if kv:
            r = subprocess.run(
                f"az keyvault secret show --vault-name {kv} --name grafana-admin-password --query value -o tsv",
                shell=True, capture_output=True, text=True, encoding="utf-8",
            )
            if r.returncode == 0:
                grafana_pw = r.stdout.strip()
                state["data"]["grafana_admin_password"] = grafana_pw
    nginx_ip     = state["data"].get("appgw_ip", "")
    argocd_url   = f"https://{nginx_ip}" if nginx_ip else "(nginx LB IP pending)"
    grafana_url  = f"http://{nginx_ip}/grafana" if nginx_ip else "(nginx LB IP pending)"
    sep = "─" * 56
    print(f"\n  {green('ACCESS SUMMARY')}")
    print(f"  {sep}")
    print(f"  ArgoCD   {argocd_url}")
    print(f"           username: admin   password: {argocd_pw or '(check argocd-initial-admin-secret)'}")
    print(f"  Grafana  {grafana_url}")
    print(f"           username: admin   password: {grafana_pw or '(check grafana-admin-secret)'}")
    print(f"  {sep}\n")

    # Auto-request vCPU quota increase if needed (Pay-As-You-Go accounts auto-approve small requests)
    _ensure_vcpu_quota(state, vcpus_needed=8)

    # Ensure at least 1 user node exists — workload pods can't schedule on the system pool
    _ensure_user_nodes(state)

    # Try ArgoCD CLI login for richer status checks
    argocd_pw = state["data"].get("argocd_password", "")
    appgw_ip  = state["data"].get("appgw_ip", "")
    if argocd_pw and appgw_ip:
        run(
            f"argocd login {appgw_ip} --username admin --password {argocd_pw} "
            f"--grpc-web --grpc-web-root-path /argocd --insecure",
            check=False,
        )

    # Ensure argocd-cm has kustomize load-restrictor disabled (may have been
    # applied in Phase 7, but on a re-run we verify and re-apply if missing)
    cm_check = subprocess.run(
        "kubectl get configmap argocd-cm -n argocd "
        "-o jsonpath='{.data.kustomize\\.buildOptions}'",
        shell=True, capture_output=True, text=True, encoding="utf-8",
    )
    if "LoadRestrictionsNone" not in (cm_check.stdout or ""):
        log("WARN", "argocd-cm missing kustomize.buildOptions — patching now …")
        subprocess.run(
            "kubectl patch configmap argocd-cm -n argocd --type=merge "
            '--patch \'{"data":{"kustomize.buildOptions":"--load-restrictor LoadRestrictionsNone"}}\'',
            shell=True, capture_output=True,
        )
        subprocess.run(
            "kubectl rollout restart deployment argocd-repo-server -n argocd",
            shell=True, capture_output=True,
        )
        subprocess.run(
            "kubectl rollout status deployment argocd-repo-server -n argocd --timeout=120s",
            shell=True,
        )
        # app-controller holds gRPC connections to repo-server pod by IP;
        # after repo-server restarts the old connections go stale → all apps
        # show "rpc" errors until app-controller reconnects.  Restart it now.
        subprocess.run(
            "kubectl rollout restart statefulset argocd-application-controller -n argocd",
            shell=True, capture_output=True,
        )
        subprocess.run(
            "kubectl rollout status statefulset argocd-application-controller -n argocd --timeout=180s",
            shell=True,
        )
        log("OK", "argocd-repo-server + application-controller restarted")
    else:
        log("OK", "argocd-cm kustomize.buildOptions already set — OK")

    # Hard-refresh all apps so app-controller re-evaluates with fresh connections
    subprocess.run(
        "kubectl annotate applications -n argocd --all "
        "argocd.argoproj.io/refresh=hard --overwrite",
        shell=True, capture_output=True,
    )
    log("INFO", "Hard-refreshed all apps — waiting 20s for re-evaluation …")
    time.sleep(20)

    # Expected applications in wave order
    expected = [
        # wave 0
        "cert-manager", "monitoring-alloy-config",
        # wave 1
        "cert-manager-config", "ingress-nginx",
        "monitoring-mimir", "monitoring-loki", "monitoring-tempo", "monitoring-alloy",
        # wave 2
        "ingresses",
        "monitoring-grafana", "monitoring-prometheus-adapter", "argo-rollouts",
        # wave 3
        "peerisland-app",
    ]

    def _argocd_app_status():
        r = subprocess.run(
            "kubectl get applications -n argocd -o json",
            shell=True, capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {}
        try:
            items = json.loads(r.stdout).get("items", [])
        except Exception:
            return {}
        return {a["metadata"]["name"]: a.get("status", {}) for a in items}

    def _has_global_rpc_error(status: dict) -> bool:
        """Return True if most apps share a connection rpc error (Unavailable) — signals a component issue.
        Does NOT count code=Unknown (build failures) since restarting the controller won't fix those."""
        rpc_count = 0
        for name in expected:
            s = status.get(name, {})
            for cond in s.get("conditions", []):
                msg = cond.get("message", "").lower()
                is_conn_error = (
                    "code = unavailable" in msg
                    or ("rpc" in msg and "code = unknown" not in msg and "failed to generate manifest" not in msg)
                )
                if is_conn_error:
                    rpc_count += 1
                    break
        return rpc_count >= max(3, len(expected) // 2)

    def _fix_global_rpc(_status: dict):
        """Delegate to the top-level shared helper."""
        _argocd_global_rpc_check_and_fix()

    _kustomize_fix_done = [False]  # mutable flag visible to nested functions

    def _diagnose_and_fix_unhealthy(status: dict):
        """
        Inspect every unhealthy app's conditions and apply targeted fixes.
        Called periodically during the wait loop so each issue is fixed once.
        """
        if _has_global_rpc_error(status):
            _fix_global_rpc(status)
            return

        for name in expected:
            s      = status.get(name, {})
            sync   = s.get("sync",   {}).get("status", "Unknown")
            health = s.get("health", {}).get("status", "Unknown")
            if sync == "Synced" and health in ("Healthy", "Progressing"):
                continue  # already fine

            # Collect every condition/health message for pattern matching
            cond_msgs = [c.get("message", "") for c in s.get("conditions", [])]
            health_msg = s.get("health", {}).get("message", "")
            all_text   = " ".join(cond_msgs + [health_msg]).lower()

            log("INFO", f"  Diagnosing {name}  sync={sync}  health={health}")
            if cond_msgs:
                for m in cond_msgs[:2]:
                    if m:
                        log("INFO", f"    condition: {m[:120]}")
            if health_msg:
                log("INFO", f"    health:    {health_msg[:120]}")

            # ── Fix 1: kustomize path / load-restrictor ───────────────────
            if (not _kustomize_fix_done[0] and (
                "load-restrictor" in all_text
                or ("kustomize" in all_text and ("not permitted" in all_text
                                                 or "outside" in all_text
                                                 or "failed to" in all_text))
            )):
                log("WARN", f"  {name}: kustomize path error — re-patching argocd-cm …")
                subprocess.run(
                    "kubectl patch configmap argocd-cm -n argocd --type=merge "
                    '--patch \'{"data":{"kustomize.buildOptions":'
                    '"--load-restrictor LoadRestrictionsNone"}}\'',
                    shell=True, capture_output=True,
                )
                subprocess.run("kubectl rollout restart deployment argocd-repo-server -n argocd",
                                shell=True, capture_output=True)
                subprocess.run("kubectl rollout status deployment argocd-repo-server -n argocd --timeout=120s",
                                shell=True)
                subprocess.run("kubectl rollout restart statefulset argocd-application-controller -n argocd",
                                shell=True, capture_output=True)
                subprocess.run("kubectl rollout status statefulset argocd-application-controller -n argocd --timeout=180s",
                                shell=True)
                subprocess.run("kubectl annotate applications -n argocd --all "
                                "argocd.argoproj.io/refresh=hard --overwrite",
                                shell=True, capture_output=True)
                _kustomize_fix_done[0] = True
                log("OK", "  argocd-cm patched — repo-server + app-controller restarted")
                time.sleep(20)
                return  # let the status refresh before fixing more

            # ── Fix 2: rpc error on individual app (not global) ───────────
            elif "rpc" in all_text or "failed to generate manifest" in all_text:
                log("WARN", f"  {name}: rpc/manifest error — hard-refreshing …")
                subprocess.run(
                    f"kubectl annotate application {name} -n argocd "
                    f"argocd.argoproj.io/refresh=hard --overwrite",
                    shell=True, capture_output=True,
                )

            # ── Fix 3: image pull failure — images not in ACR yet ─────────
            elif "imagepullbackoff" in all_text or "errimagepull" in all_text or "not found" in all_text:
                log("WARN", f"  {name}: ImagePull error — images may not be in ACR yet (waiting for build)")

            # ── Fix 4: OutOfSync — trigger a sync ─────────────────────────
            elif sync == "OutOfSync":
                log("WARN", f"  {name}: OutOfSync — triggering sync …")
                subprocess.run(
                    f"argocd app sync {name} --force --async",
                    shell=True, capture_output=True,
                )
                subprocess.run(
                    f"kubectl annotate application {name} -n argocd "
                    f"argocd.argoproj.io/refresh=hard --overwrite",
                    shell=True, capture_output=True,
                )

            # ── Fix 5: Unknown sync — hard refresh ────────────────────────
            elif sync == "Unknown":
                log("WARN", f"  {name}: Unknown sync — hard-refreshing …")
                subprocess.run(
                    f"kubectl annotate application {name} -n argocd "
                    f"argocd.argoproj.io/refresh=hard --overwrite",
                    shell=True, capture_output=True,
                )

            # ── Fix 6: Degraded — log pod errors for visibility ───────────
            elif health == "Degraded":
                # Map app → namespace for pod inspection
                ns_map = {
                    "monitoring-grafana":            "monitoring",
                    "monitoring-loki":               "monitoring",
                    "monitoring-mimir":              "monitoring",
                    "monitoring-tempo":              "monitoring",
                    "monitoring-alloy":              "monitoring",
                    "monitoring-alloy-config":       "monitoring",
                    "monitoring-prometheus-adapter": "monitoring",
                    "peerisland-app":                "peerisland",
                    "ingress-nginx":                 "ingress-nginx",
                    "cert-manager":                  "cert-manager",
                    "cert-manager-config":           "cert-manager",
                    "argo-rollouts":                 "argo-rollouts",
                    "ingresses":                     "peerisland",
                }
                ns = ns_map.get(name, "")
                if ns:
                    pods_r = subprocess.run(
                        f"kubectl get pods -n {ns} --no-headers "
                        f"-o custom-columns="
                        f"NAME:.metadata.name,"
                        f"STATUS:.status.phase,"
                        f"READY:.status.containerStatuses[0].ready,"
                        f"REASON:.status.containerStatuses[0].state.waiting.reason",
                        shell=True, capture_output=True, text=True,
                    )
                    for pod_line in (pods_r.stdout or "").strip().splitlines()[:5]:
                        if pod_line.strip():
                            log("INFO", f"    pod: {pod_line.strip()[:100]}")

    def _force_sync_stuck(status: dict):
        """Force-refresh and sync apps that are Unknown or OutOfSync."""
        _diagnose_and_fix_unhealthy(status)

    def all_healthy():
        status = _argocd_app_status()
        if not status:
            return False

        total  = len(expected)
        done   = 0
        rows   = []
        for name in expected:
            s      = status.get(name, {})
            sync   = s.get("sync",   {}).get("status", "Unknown")
            health = s.get("health", {}).get("status", "Unknown")
            ok     = sync == "Synced" and health in ("Healthy", "Progressing")
            if ok:
                done += 1
            # Grab first ComparisonError / degraded message for display
            msg = ""
            for cond in s.get("conditions", []):
                if cond.get("type") in ("ComparisonError", "SyncError"):
                    msg = cond.get("message", "")[:80]
                    break
            if not msg and health == "Degraded":
                msg = s.get("health", {}).get("message", "")[:80]
            icon = green("✓") if ok else yellow("…")
            rows.append((icon, name, sync, health, ok, msg))

        ts = datetime.now().strftime("%H:%M:%S")
        not_ready = [name for (_, name, _, _, ok, _) in rows if not ok]
        summary   = f"{done}/{total} healthy"
        if not_ready:
            summary += f"  —  waiting: {', '.join(not_ready)}"
        print(f"\n{dim(ts)}  {'·'}  {bold(summary)}")
        for (icon, name, sync, health, _, msg) in rows:
            line = f"    {icon}  {name:40}  {dim(sync + '/' + health)}"
            if msg:
                line += f"  {yellow(msg)}"
            print(line)

        return done == total

    # First pass: wait up to 10 min
    deadline = time.time() + 600
    interval = 20
    while time.time() < deadline:
        if all_healthy():
            log("OK", "All ArgoCD apps Synced/Healthy")
            break
        remaining = int(deadline - time.time())
        print(f"  {dim(f'  … {remaining}s left')}", end="\r", flush=True)
        time.sleep(interval)
    else:
        # Timeout — force-sync stuck apps and wait a final 10 min
        print()
        log("WARN", "Timeout — force-syncing stuck apps and retrying for 10 more minutes …")
        status = _argocd_app_status()
        _force_sync_stuck(status)
        time.sleep(10)
        deadline2 = time.time() + 600
        while time.time() < deadline2:
            if all_healthy():
                log("OK", "All ArgoCD apps Synced/Healthy (after force-sync)")
                break
            remaining = int(deadline2 - time.time())
            print(f"  {dim(f'  … {remaining}s left')}", end="\r", flush=True)
            time.sleep(interval)
        else:
            print()
            log("WARN", "Some apps still not healthy — continuing to access summary anyway")
            all_healthy()  # print final state

    # ── Print access summary ─────────────────────────────────────────────────
    nginx_ip = capture(
        "kubectl get svc ingress-nginx-controller -n ingress-nginx "
        "-o jsonpath='{.status.loadBalancer.ingress[0].ip}'",
        check=False,
    ).strip().strip("'")
    argocd_pass_b64 = capture(
        "kubectl -n argocd get secret argocd-initial-admin-secret "
        "-o jsonpath='{.data.password}'",
        check=False,
    ).strip().strip("'")
    if argocd_pass_b64:
        import base64 as _b64
        try:
            argocd_pass = _b64.b64decode(argocd_pass_b64).decode()
        except Exception:
            argocd_pass = argocd_pass_b64
    else:
        argocd_pass = state["data"].get("argocd_password", "<check argocd-initial-admin-secret>")

    grafana_pass = state["data"].get("grafana_admin_password", "<check grafana-admin-secret in monitoring ns>")
    argocd_url   = f"https://{nginx_ip}" if nginx_ip else "<nginx LB IP not yet assigned>"
    grafana_url  = f"http://{nginx_ip}/grafana" if nginx_ip else "<nginx LB IP not yet assigned>"

    state["data"]["argocd_url"]      = argocd_url
    state["data"]["argocd_password"] = argocd_pass

    sep = "─" * 60
    print(f"\n  {green('ACCESS SUMMARY')}")
    print(f"  {sep}")
    print(f"  ArgoCD   {argocd_url}")
    print(f"           username: admin   password: {argocd_pass}")
    print(f"  {sep}")
    print(f"  Grafana  {grafana_url}")
    print(f"           username: admin   password: {grafana_pass}")
    print(f"  {sep}\n")

    mark_done(state, 8)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 9 — Trigger first build (GitHub Actions)
# ═════════════════════════════════════════════════════════════════════════════

def _oidc_tf_apply():
    """Run terraform apply scoped to the OIDC resources then ensure the SP exists.

    Targets only the three OIDC resources so it's fast and doesn't touch AKS/ACR/VNet.
    After apply, verifies the service principal was created (Azure AD replication can lag)
    and creates it via `az ad sp create` if Terraform silently skipped it.
    """
    tf_dir = REPO_ROOT / "terraform"
    log("INFO", "  Running terraform apply on OIDC resources …")
    rc, out = _tf_stream(
        "terraform apply "
        "-target=azuread_application.cicd "
        "-target=azuread_service_principal.cicd "
        "-target=azuread_application_federated_identity_credential.github_main "
        "-auto-approve",
        str(tf_dir),
    )
    if rc != 0:
        log("WARN", f"  terraform apply failed (exit {rc})")
        # 409 Conflict: credential with name github-main already exists in Azure AD
        # but not in Terraform state (happens after terraform state rm).
        # Delete the orphaned credential from Azure AD and retry once.
        if "409" in out or "already exists" in out.lower():
            log("WARN", "  409 Conflict on federated credential — deleting orphaned credential and retrying …")
            client_id_now, obj_id_now = _tf_cicd_app_ids()
            app_id_for_del = obj_id_now or client_id_now
            if app_id_for_del:
                creds_raw = capture(
                    f"az ad app federated-credential list --id {app_id_for_del} -o json",
                    check=False,
                )
                for cred in json.loads(creds_raw or "[]"):
                    subprocess.run(
                        f"az ad app federated-credential delete "
                        f"--id {app_id_for_del} "
                        f"--federated-credential-id {cred['id']}",
                        shell=True, capture_output=True,
                    )
                    log("INFO", f"  Deleted orphaned credential {cred.get('name')}")
                rc, out = _tf_stream(
                    "terraform apply "
                    "-target=azuread_application.cicd "
                    "-target=azuread_service_principal.cicd "
                    "-target=azuread_application_federated_identity_credential.github_main "
                    "-auto-approve",
                    str(tf_dir),
                )
                if rc == 0:
                    log("OK", "  terraform apply succeeded on retry")
                else:
                    log("WARN", f"  retry also failed: {out[-300:]}")
        else:
            log("WARN", f"  last output: {out[-400:]}")
    else:
        log("OK", "  terraform apply succeeded")

    # Verify the SP was actually created.
    # Azure AD needs time to propagate a new application before the SP can be
    # created against it — Terraform's first apply may silently skip the SP.
    # Retry the apply (up to 3×, waiting 30 s between) so Terraform creates it.
    client_id, _ = _tf_cicd_app_ids()
    if not client_id:
        log("WARN", "  No client_id after apply — cannot verify SP")
        return

    for attempt in range(1, 4):
        sp_check = subprocess.run(
            f"az ad sp show --id {client_id} --query appId -o tsv",
            shell=True, capture_output=True, text=True,
        )
        if sp_check.returncode == 0 and sp_check.stdout.strip():
            log("OK", f"  Service principal exists for {client_id[:8]}…")
            return

        if attempt < 3:
            log("WARN",
                f"  SP not yet visible (attempt {attempt}/3) — "
                f"waiting 30 s for Azure AD propagation then retrying terraform apply …")
            time.sleep(30)
            rc2, out2 = _tf_stream(
                "terraform apply "
                "-target=azuread_application.cicd "
                "-target=azuread_service_principal.cicd "
                "-target=azuread_application_federated_identity_credential.github_main "
                "-auto-approve",
                str(tf_dir),
            )
            if rc2 != 0:
                log("WARN", f"  retry apply failed (exit {rc2}): {out2[-200:]}")

    log("WARN",
        f"  SP still missing for {client_id[:8]}… after 3 terraform apply attempts.\n"
        f"  Run 'terraform apply' manually in terraform/ to let Terraform create it.")


def _refresh_github_secrets(state: dict, org: str, repo_name: str):
    """Re-push all GitHub Actions secrets and fix the OIDC federated credential.

    Phase 3 runs before Terraform so secrets may carry empty placeholders.
    GitHub OIDC tokens include internal numeric IDs in the subject claim
    (repo:org@userId/repo@repoId:ref:...) which must match the Azure AD
    federated credential exactly — Terraform sets the plain form, we fix it here.
    """
    tf_dir = REPO_ROOT / "terraform"
    tf_outs_raw = capture("terraform output -json", cwd=tf_dir, check=False)
    tf_outs = json.loads(tf_outs_raw) if tf_outs_raw.strip().startswith("{") else {}

    def tf(key: str) -> str:
        return tf_outs.get(key, {}).get("value", "") or ""

    client_id, _app_obj_id_cache = _tf_cicd_app_ids()
    client_id = client_id or tf("cicd_client_id")
    tenant_id  = tf("cicd_tenant_id")  or state["data"].get("tenant_id", "")
    sub_id     = tf("cicd_subscription_id") or state["data"].get("subscription_id", "")
    acr_name   = state["data"].get("acr_name",          "peerislandprodacr")
    acr_server = state["data"].get("acr_login_server",  f"{acr_name}.azurecr.io")
    aks_name   = state["data"].get("aks_cluster_name",  "peerisland-prod-aks")
    aks_rg     = state["data"].get("aks_resource_group","peerisland-prod-rg")

    slug = f"{org}/{repo_name}"

    # ── GitHub Actions secrets ────────────────────────────────────────────────
    def _set(name: str, val: str):
        if not val:
            log("WARN", f"  {name}: empty — skipping")
            return
        r = subprocess.run(
            f'gh secret set {name} --repo {slug} --body "{val}"',
            shell=True, capture_output=True, text=True,
        )
        icon = green("✓") if r.returncode == 0 else yellow("!")
        log("OK" if r.returncode == 0 else "WARN", f"  {icon} {name}")

    log("INFO", "Refreshing GitHub Actions secrets from Terraform outputs …")
    for name, val in [
        ("AZURE_CLIENT_ID",       client_id),
        ("AZURE_TENANT_ID",       tenant_id),
        ("AZURE_SUBSCRIPTION_ID", sub_id),
        ("ACR_NAME",              acr_name),
        ("ACR_LOGIN_SERVER",      acr_server),
        ("AKS_CLUSTER_NAME",      aks_name),
        ("AKS_RESOURCE_GROUP",    aks_rg),
    ]:
        _set(name, val)

    if not client_id:
        log("WARN", "AZURE_CLIENT_ID still empty — Terraform may not have run yet. Re-run after phase 4.")
        return
    log("OK", f"GitHub Actions secrets up to date  (client_id={client_id[:8]}…)")

    # ── Orphan Azure AD app cleanup ───────────────────────────────────────────
    # Failed Terraform runs create extra apps with the same display name.
    # az ad app list returns an arbitrary one, most with no federated credential.
    # Delete all orphans so only the Terraform-managed app remains.
    log("INFO", "Checking for orphan Azure AD apps …")
    try:
        _, tf_obj_id = _tf_cicd_app_ids()

        # List all apps with the same display name
        all_apps_raw = capture(
            "az ad app list --display-name peerisland-prod-cicd "
            "--query \"[].{appId:appId,id:id}\" -o json",
            check=False,
        )
        all_apps = json.loads(all_apps_raw) if all_apps_raw.strip().startswith("[") else []

        if len(all_apps) > 1:
            for app in all_apps:
                if app["id"] == tf_obj_id:
                    continue  # keep the Terraform-managed one
                log("WARN", f"Deleting orphan app {app['appId']} (objectId={app['id']}) …")
                subprocess.run(
                    f"az ad app delete --id {app['id']}",
                    shell=True, capture_output=True,
                )
            log("OK", "Orphan apps removed")
        else:
            log("OK", f"No orphan apps found ({len(all_apps)} app(s) with this name)")
    except Exception as exc:
        log("WARN", f"Orphan cleanup skipped: {exc}")

    # ── OIDC federated credential subject fix ────────────────────────────────
    # GitHub OIDC tokens embed internal numeric IDs in the subject claim:
    #   repo:ORG@USER_ID/REPO@REPO_ID:ref:refs/heads/main
    # The Azure AD federated credential must match this exactly.
    # Terraform sets the plain form (no IDs); we detect and fix it here.
    log("INFO", "Verifying Azure AD OIDC federated credential …")
    try:
        user_id = subprocess.run(
            f"gh api users/{org} --jq .id",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        repo_id = subprocess.run(
            f"gh api repos/{org}/{repo_name} --jq .id",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        if not user_id or not repo_id:
            log("WARN", "Could not fetch GitHub user/repo IDs — skipping OIDC subject fix")
            return

        correct_subject = f"repo:{org}@{user_id}/{repo_name}@{repo_id}:ref:refs/heads/main"
        log("INFO", f"  expected subject : {correct_subject}")

        _, app_obj_id = _tf_cicd_app_ids()
        log("INFO", f"  app object ID    : {app_obj_id or '(not found in state)'}")
        if not app_obj_id:
            log("WARN", "No app object ID in Terraform state — running terraform apply to recreate …")
            _oidc_tf_apply()
            _, app_obj_id = _tf_cicd_app_ids()
            if not app_obj_id:
                log("WARN", "Still no app after terraform apply — giving up on OIDC fix")
                return

        # Check the app actually exists in Azure AD (state can be stale)
        app_check = subprocess.run(
            f"az ad app show --id {app_obj_id} --query id -o tsv",
            shell=True, capture_output=True, text=True,
        )
        if app_check.returncode != 0:
            log("WARN",
                f"  App {app_obj_id} does not exist in Azure AD — Terraform state is stale\n"
                f"  Removing stale state entries and re-applying …")
            tf_dir = REPO_ROOT / "terraform"
            # Before removing the federated credential from Terraform state, also
            # delete it from Azure AD (if it somehow still exists there under the new app).
            # Without this, terraform apply gets a 409 Conflict on the credential.
            # We don't know the new app's object_id yet, so delete by display-name search.
            for existing_app in json.loads(
                capture(
                    "az ad app list --display-name peerisland-prod-cicd "
                    '--query "[].id" -o json',
                    check=False,
                ) or "[]"
            ):
                try:
                    creds_raw = capture(
                        f"az ad app federated-credential list --id {existing_app} -o json",
                        check=False,
                    )
                    for cred in json.loads(creds_raw or "[]"):
                        subprocess.run(
                            f"az ad app federated-credential delete "
                            f"--id {existing_app} "
                            f"--federated-credential-id {cred['id']}",
                            shell=True, capture_output=True,
                        )
                        log("INFO", f"  Deleted stale credential {cred.get('name')} from app {existing_app[:8]}…")
                except Exception:
                    pass
            for res in [
                "azuread_application_federated_identity_credential.github_main",
                "azuread_service_principal.cicd",
                "azuread_application.cicd",
            ]:
                subprocess.run(
                    f"terraform state rm {res}",
                    shell=True, cwd=str(tf_dir), capture_output=True,
                )
            _oidc_tf_apply()  # also creates SP if missing (see helper)
            new_client_id, app_obj_id = _tf_cicd_app_ids()
            if not app_obj_id:
                log("WARN", "Could not recover app after state cleanup — giving up")
                return
            log("OK", f"  App recreated, new object ID: {app_obj_id}")
            # Update AZURE_CLIENT_ID secret with the new client_id
            if new_client_id and new_client_id != client_id:
                log("INFO", f"  New client_id={new_client_id[:8]}… — refreshing AZURE_CLIENT_ID secret")
                subprocess.run(
                    f'gh secret set AZURE_CLIENT_ID --repo {org}/{repo_name} --body "{new_client_id}"',
                    shell=True, capture_output=True,
                )
                client_id = new_client_id

        # List credentials on the (now-verified) app
        creds_raw = capture(
            f"az ad app federated-credential list --id {app_obj_id} -o json",
            check=False,
        )
        log("INFO", f"  credential list  : {creds_raw[:120] if creds_raw else '(empty)'}")
        creds = json.loads(creds_raw) if creds_raw.strip().startswith("[") else []
        main_cred = next(
            (c for c in creds if "main" in c.get("subject", "").lower()),
            creds[0] if creds else None,
        )

        if not main_cred:
            log("WARN", "  No federated credential found — running targeted terraform apply …")
            _oidc_tf_apply()
            creds_raw = capture(
                f"az ad app federated-credential list --id {app_obj_id} -o json",
                check=False,
            )
            creds = json.loads(creds_raw) if creds_raw.strip().startswith("[") else []
            main_cred = next(
                (c for c in creds if "main" in c.get("subject", "").lower()),
                creds[0] if creds else None,
            )
            if not main_cred:
                log("WARN", "  Federated credential still missing after terraform apply — OIDC will fail")
                return
            log("OK", "  Federated credential recreated by Terraform")

        current_subject = main_cred.get("subject", "")
        log("INFO", f"  current subject  : {current_subject}")

        if current_subject == correct_subject:
            log("OK", "  OIDC subject matches — no fix needed")
            return

        log("WARN",
            f"  Subject mismatch — fixing:\n"
            f"    have: {current_subject}\n"
            f"    need: {correct_subject}")

        import tempfile as _tmp
        subprocess.run(
            f"az ad app federated-credential delete --id {app_obj_id} "
            f"--federated-credential-id {main_cred['id']}",
            shell=True, capture_output=True,
        )
        with _tmp.NamedTemporaryFile(mode="w", suffix=".json",
                                     delete=False, encoding="utf-8") as f:
            json.dump({
                "name":        main_cred.get("name", "github-main"),
                "issuer":      "https://token.actions.githubusercontent.com",
                "subject":     correct_subject,
                "audiences":   ["api://AzureADTokenExchange"],
                "description": "GitHub Actions pushes to main branch",
            }, f)
            tmp_path = f.name
        r = subprocess.run(
            f"az ad app federated-credential create --id {app_obj_id} "
            f'--parameters "@{tmp_path}"',
            shell=True, capture_output=True, text=True,
        )
        Path(tmp_path).unlink(missing_ok=True)
        if r.returncode == 0:
            log("OK", f"  Federated credential recreated → {correct_subject}")
            log("INFO", "  Waiting 2 min for Azure AD global propagation …")
            time.sleep(120)
        else:
            log("WARN", f"  Credential recreate failed: {(r.stderr or r.stdout)[:200]}")
    except Exception as exc:
        log("WARN", f"OIDC subject check failed: {exc}")


# ═════════════════════════════════════════════════════════════════════════════
#  ArgoCD Live Status Widget  (Phase 9 → 11)
# ═════════════════════════════════════════════════════════════════════════════

class _ArgoStatusWidget:
    """
    Sticky top widget showing live ArgoCD app health/sync.

    Uses ANSI scroll-region to pin HEIGHT rows at the top of the terminal
    while bootstrap output scrolls below.  Refreshes every 12 s in a
    background daemon thread.  Safe to call start() / stop() from any phase.
    Does nothing when stdout is not a tty (CI, piped output).
    """

    HEIGHT = 7  # rows the widget occupies (title + sep + 4 app rows + sep)

    def __init__(self):
        self._stop   = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock   = threading.Lock()
        sz = shutil.get_terminal_size((80, 24))
        self._cols = sz.columns
        self._rows = sz.lines

    # ── public ────────────────────────────────────────────────────────────────

    def start(self):
        if not sys.stdout.isatty():
            return
        H, rows = self.HEIGHT, self._rows
        with self._lock:
            # Pin rows 1..H; let rows H+1..rows scroll normally
            sys.stdout.write(
                f"\033[{H + 1};{rows}r"  # scroll region
                f"\033[{H + 1};1H"       # cursor → start of scroll area
            )
            sys.stdout.flush()
        self._refresh()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if not sys.stdout.isatty():
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)  # don't wait long — just let it finish
        with self._lock:
            # \033[r  — reset scroll region to full screen
            # \0337   — save cursor (DEC)
            # \033[1;1H — go to widget top
            # \033[2K\033[1B × HEIGHT — erase each row, step down (no scroll)
            # \0338   — restore saved cursor
            out = "\033[r\0337\033[1;1H"
            for _ in range(self.HEIGHT):
                out += "\033[2K\033[1B"
            out += "\0338"
            sys.stdout.write(out)
            sys.stdout.flush()

    # ── internals ─────────────────────────────────────────────────────────────

    def _run(self):
        while not self._stop.wait(12):
            self._refresh()

    def _refresh(self):
        try:
            r = subprocess.run(
                "kubectl get applications -n argocd "
                "-o jsonpath='"
                "{range .items[*]}"
                "{.metadata.name},{.status.health.status},{.status.sync.status}\\n"
                "{end}'",
                shell=True, capture_output=True, text=True, timeout=8,
            )
            apps = []
            for ln in r.stdout.strip().splitlines():
                p = ln.split(",")
                if len(p) >= 3:
                    apps.append({"name": p[0], "health": p[1] or "Unknown",
                                 "sync": p[2] or "Unknown"})

            total   = len(apps)
            healthy = sum(1 for a in apps if a["health"] == "Healthy")
            synced  = sum(1 for a in apps if a["sync"]   == "Synced")
            ts      = datetime.now().strftime("%H:%M:%S")

            if total == 0:
                overall = dim("(no apps yet)")
            elif healthy == total:
                overall = f"{GREEN}{BOLD}✓ All {total} Healthy & Synced{RESET}"
            else:
                h = GREEN if healthy == total else YELLOW
                s = GREEN if synced  == total else YELLOW
                overall = f"{h}{healthy}/{total} Healthy{RESET}  {s}{synced}/{total} Synced{RESET}"

            # Unhealthy/unsynced first, then fill with healthy — show max 4
            bad  = [a for a in apps if a["health"] != "Healthy" or a["sync"] != "Synced"]
            good = [a for a in apps if a not in bad]
            shown = (bad + good)[:4]

            def _row(a: dict) -> str:
                ok     = a["health"] == "Healthy" and a["sync"] == "Synced"
                icon   = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
                hc     = GREEN if a["health"] == "Healthy" else YELLOW
                sc     = GREEN if a["sync"]   == "Synced"  else YELLOW
                return (f"  {icon} {a['name']:<42} "
                        f"{hc}{a['health']:<12}{RESET}  {sc}{a['sync']}{RESET}")

            sep = dim("─" * min(self._cols - 4, 72))
            widget_rows = [
                f"  {BOLD}ArgoCD{RESET}  {overall}  {dim(ts)}",
                f"  {sep}",
            ]
            for a in shown:
                widget_rows.append(_row(a))
            while len(widget_rows) < self.HEIGHT - 1:
                widget_rows.append("")
            widget_rows.append(f"  {sep}")

            # Atomically overwrite the pinned widget area
            with self._lock:
                out = "\033[s\033[1;1H"
                for ln in widget_rows:
                    out += f"\033[2K{ln}\r\n"
                out += "\033[u"
                sys.stdout.write(out)
                sys.stdout.flush()
        except Exception:
            pass


# Singleton — started once at Phase 9, lives until bootstrap exits
_argocd_widget: "_ArgoStatusWidget | None" = None


def _start_argocd_widget():
    """Start the live ArgoCD widget — idempotent."""
    global _argocd_widget
    if _argocd_widget is None and sys.stdout.isatty():
        _argocd_widget = _ArgoStatusWidget()

        import atexit, signal

        # atexit covers normal exits and unhandled exceptions
        atexit.register(_argocd_widget.stop)

        # SIGINT (Ctrl+C) — stop widget synchronously then re-raise so Python
        # prints the standard "^C" / exits with code 130
        _prev_sigint = signal.getsignal(signal.SIGINT)
        def _sigint_handler(sig, frame):
            _argocd_widget.stop()
            if callable(_prev_sigint):
                _prev_sigint(sig, frame)
            else:
                raise KeyboardInterrupt
        signal.signal(signal.SIGINT, _sigint_handler)

        _argocd_widget.start()


def _print_argocd_status_table(label: str = "ArgoCD Apps"):
    """Print a one-line-per-app status table. Called at Phase 8 start and after fixes."""
    r = subprocess.run(
        "kubectl get applications -n argocd -o json",
        shell=True, capture_output=True, text=True,
    )
    if r.returncode != 0:
        log("WARN", f"kubectl get applications failed: {r.stderr.strip()[:120]}")
        return {}
    try:
        items = json.loads(r.stdout).get("items", [])
    except Exception:
        return {}

    rows = []
    for a in sorted(items, key=lambda x: x["metadata"]["name"]):
        name   = a["metadata"]["name"]
        s      = a.get("status", {})
        sync   = s.get("sync",   {}).get("status", "Unknown")
        health = s.get("health", {}).get("status", "Unknown")
        ok     = sync == "Synced" and health in ("Healthy", "Progressing")
        cond_msgs = [c.get("message", "") for c in s.get("conditions", [])]
        hmsg      = s.get("health", {}).get("message", "")
        errs      = [m.strip() for m in (cond_msgs + [hmsg]) if m.strip()]
        rows.append((name, sync, health, ok, errs))

    name_w   = max((len(r[0]) for r in rows), default=20) + 2
    sync_w   = max((len(r[1]) for r in rows), default=10)
    health_w = max((len(r[2]) for r in rows), default=10)
    sep      = "-" * (name_w + sync_w + health_w + 10)

    print(f"\n  {bold(label)}  ({len(items)} found)")
    print(f"  {sep}")
    hdr = f"  {'APP':<{name_w}}  {'SYNC':<{sync_w}}  {'HEALTH':<{health_w}}"
    print(bold(hdr) if _USE_COLOR else hdr)
    print(f"  {sep}")
    for name, sync, health, ok, errs in rows:
        status_col = f"{sync:<{sync_w}}  {health:<{health_w}}"
        if ok:
            line = f"  {green('OK')}  {name:<{name_w}}  {status_col}"
        else:
            line = f"  {red('!!')}  {name:<{name_w}}  {status_col}"
        print(line)
        for e in errs[:1]:
            # Trim the error to something readable — strip long path prefixes
            short = e
            for prefix in ("Failed to load target state: failed to generate manifest for source 1 of 1: ",
                           "Failed to load target state: failed to generate manifest for source 1 of 2: ",
                           "Failed sync attempt to v"):
                if short.startswith(prefix):
                    short = short[len(prefix):]
                    break
            print(f"      -> {short[:110]}")
    print(f"  {sep}\n")
    return {a["metadata"]["name"]: a.get("status", {}) for a in items}


def _ensure_argocd_resources_patched():
    """
    Apply (or re-apply) argocd-values.yaml via helm upgrade so that resource
    limits, repo-server replicas=2, and --repo-server-timeout-seconds are live.
    Idempotent — helm upgrade --install is a no-op when nothing changed.

    Also explicitly patches argocd-cm with kustomize.buildOptions because
    helm does not reliably update an already-existing argocd-cm ConfigMap.
    """
    argocd_values = REPO_ROOT / "argocd" / "argocd-values.yaml"
    if not argocd_values.exists():
        log("WARN", "argocd/argocd-values.yaml not found — skipping resource patch")
        return
    log("INFO", "Applying ArgoCD resource limits + repo-server replicas via helm upgrade …")
    r = subprocess.run(
        f'helm upgrade --install argocd argo/argo-cd '
        f'--namespace argocd '
        f'--version 6.7.3 '
        f'--values "{argocd_values}" '
        f'--wait --timeout 5m',
        shell=True, capture_output=True, text=True,
    )
    if r.returncode == 0:
        log("OK", "ArgoCD Helm values applied — resource limits and repo-server replicas=2 are now live")
    else:
        log("WARN", f"helm upgrade returned non-zero: {r.stderr.strip()[:200]}")

    # Helm does not reliably write configs.cm keys into an already-existing argocd-cm.
    # Patch explicitly so kustomize ../../ path traversal works for the peerisland overlay.
    r2 = subprocess.run(
        "kubectl get cm argocd-cm -n argocd -o jsonpath={.data.kustomize\\.buildOptions}",
        shell=True, capture_output=True, text=True,
    )
    if "--load-restrictor" not in r2.stdout:
        log("INFO", "argocd-cm missing kustomize.buildOptions — patching directly …")
        subprocess.run(
            "kubectl patch configmap argocd-cm -n argocd --type merge "
            "-p '{\"data\":{\"kustomize.buildOptions\":\"--load-restrictor LoadRestrictionsNone\"}}'",
            shell=True, check=True,
        )
        log("OK", "argocd-cm patched: kustomize.buildOptions=--load-restrictor LoadRestrictionsNone")
    else:
        log("OK", "argocd-cm kustomize.buildOptions already set — no patch needed")


_argocd_resources_patched = False


def _argocd_global_rpc_check_and_fix(wait_healthy: bool = True, timeout: int = 300) -> bool:
    """
    Detect and fix a global ArgoCD rpc error, then wait until apps are no longer
    showing the error (or until *timeout* seconds).

    When argocd-repo-server restarts (e.g. OOMKilled due to missing resource limits),
    argocd-application-controller keeps stale gRPC connections → ALL apps show
    'rpc error: code = Unavailable'. Fix: patch resource limits (prevents future
    OOM restarts) + restart the controller (clears stale connections now).

    Returns True if a fix was applied.  If wait_healthy=True (default), blocks
    until the rpc error is gone before returning so callers can proceed safely.
    """
    def _get_items():
        r = subprocess.run(
            "kubectl get applications -n argocd -o json",
            shell=True, capture_output=True, text=True,
        )
        if r.returncode != 0:
            return []
        try:
            return json.loads(r.stdout).get("items", [])
        except Exception:
            return []

    def _rpc_count(items):
        # Only count Unavailable (stale gRPC connection) — NOT Unknown (kustomize/helm build failure).
        # Restarting app-controller fixes Unavailable; it does nothing for Unknown build errors.
        count = 0
        for a in items:
            for cond in a.get("status", {}).get("conditions", []):
                msg = cond.get("message", "").lower()
                is_conn_error = (
                    "code = unavailable" in msg
                    or ("rpc" in msg and "code = unknown" not in msg and "failed to generate manifest" not in msg)
                )
                if is_conn_error:
                    count += 1
                    break
        return count

    items = _get_items()
    total = len(items)
    if total == 0:
        log("WARN", "kubectl get applications returned nothing — skipping rpc check")
        return False

    rpc = _rpc_count(items)
    if rpc < max(3, total // 2):
        return False  # not a global issue

    log("WARN",
        f"Global rpc error: {rpc}/{total} apps affected — "
        f"restarting argocd-application-controller …")

    subprocess.run(
        "kubectl rollout restart statefulset argocd-application-controller -n argocd",
        shell=True,
    )
    subprocess.run(
        "kubectl rollout status statefulset argocd-application-controller -n argocd --timeout=180s",
        shell=True,
    )
    subprocess.run(
        "kubectl annotate applications -n argocd --all "
        "argocd.argoproj.io/refresh=hard --overwrite",
        shell=True, capture_output=True,
    )
    log("OK", "application-controller restarted — waiting for rpc errors to clear …")
    time.sleep(20)

    if not wait_healthy:
        return True

    # Poll until rpc errors are gone (or timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = _get_items()
        rpc = _rpc_count(items)
        if rpc == 0:
            log("OK", "rpc errors cleared — all apps re-evaluating")
            return True
        remaining = int(deadline - time.time())
        log("INFO", f"  {rpc}/{len(items)} apps still showing rpc error — waiting … ({remaining}s left)")
        time.sleep(15)

    log("WARN", f"rpc errors still present after {timeout}s — continuing anyway")
    return True


def _print_access_summary(state: dict):
    """Print ArgoCD, Grafana, and infra URLs with credentials. Called at Phase 9 end and Phase 11."""
    # Resolve nginx LB IP — may have been stored under different keys across phases
    nginx_ip = (
        state["data"].get("appgw_ip")
        or state["data"].get("nginx_ip")
        or capture(
            "kubectl get svc ingress-nginx-controller -n ingress-nginx "
            "-o jsonpath='{.status.loadBalancer.ingress[0].ip}'",
            check=False,
        ).strip().strip("'")
        or ""
    )

    # ArgoCD initial admin password — stored in state or fetched live from secret
    argocd_pw = state["data"].get("argocd_password", "")
    if not argocd_pw:
        pw_b64 = capture(
            "kubectl -n argocd get secret argocd-initial-admin-secret "
            "-o jsonpath='{.data.password}'",
            check=False,
        ).strip().strip("'")
        if pw_b64:
            try:
                import base64 as _b64
                argocd_pw = _b64.b64decode(pw_b64).decode()
                state["data"]["argocd_password"] = argocd_pw
            except Exception:
                argocd_pw = pw_b64

    # Grafana admin password — from state or Key Vault
    grafana_pw = state["data"].get("grafana_password") or state["data"].get("grafana_admin_password", "")
    if not grafana_pw:
        kv = state["data"].get("kv_name", "")
        if kv:
            r = subprocess.run(
                f"az keyvault secret show --vault-name {kv} --name grafana-admin-password --query value -o tsv",
                shell=True, capture_output=True, text=True, encoding="utf-8",
            )
            if r.returncode == 0:
                grafana_pw = r.stdout.strip()
                state["data"]["grafana_admin_password"] = grafana_pw

    base = f"https://{nginx_ip}" if nginx_ip else "(nginx LB IP not yet assigned — run after Phase 7)"

    sep = "─" * 58
    def row(label, value=""):
        return f"  {label:<26}  {value}" if value else f"  {label}"

    print(f"\n  {green('ACCESS SUMMARY')}  {dim('(save these credentials)')}")
    print(f"  {sep}")
    print(row("ArgoCD",       base))
    print(row("  username",   "admin"))
    print(row("  password",   argocd_pw or "(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"))
    print(f"  {sep}")
    print(row("Grafana",      f"{base}/grafana"))
    print(row("  username",   "admin"))
    print(row("  password",   grafana_pw or "(az keyvault secret show --vault-name <kv> --name grafana-admin-password --query value -o tsv)"))
    print(f"  {sep}")
    print(row("App",          base))
    print(row("AKS cluster",  state["data"].get("aks_cluster_name", "")))
    print(row("ACR",          state["data"].get("acr_login_server", "")))
    print(row("Key Vault",    state["data"].get("kv_name", "")))
    print(f"  {sep}\n")


def phase9_first_build(state: dict):
    phase_header(9, "Trigger first build via GitHub Actions")
    org       = state["data"].get("github_org",  "")
    repo_name = state["data"].get("github_repo", "")
    slug      = f"{org}/{repo_name}"

    # Ensure all secrets carry real post-Terraform values (Phase 3 ran before TF)
    _refresh_github_secrets(state, org, repo_name)

    # Fix global rpc before triggering the build — if app-controller is in a stale
    # state it won't sync the new images after the build completes
    _argocd_global_rpc_check_and_fix()

    result = run(
        f"gh workflow run build-push.yml --repo {slug} --ref main",
        check=False,
    )
    if result.returncode != 0:
        log("WARN", "gh workflow run failed — triggering via empty git commit instead")
        run('git commit --allow-empty -m "chore: trigger initial CI build [bootstrap]"')
        run("git push")

    log("INFO", "Waiting 20s for the run to register in GitHub…")
    time.sleep(20)

    def workflow_complete():
        r = subprocess.run(
            f"gh run list --repo {slug} --workflow=build-push.yml --limit=1 "
            f"--json status,conclusion",
            shell=True, capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False
        runs = json.loads(r.stdout)
        if not runs:
            return False
        run_info   = runs[0]
        status     = run_info.get("status",     "")
        conclusion = run_info.get("conclusion", "")
        print(f"  {dim(f'  workflow status={status}  conclusion={conclusion}')}", end="\r", flush=True)
        if status == "completed":
            if conclusion == "success":
                return True
            die(f"GitHub Actions workflow ended with conclusion={conclusion!r}. Check the Actions tab.")
        return False

    wait_for(workflow_complete, "build-push.yml workflow completed", timeout=600, interval=15)
    print()
    log("OK", "Images built, tagged, pushed to ACR. kustomization.yaml SHA bumped.")
    mark_done(state, 9)

    # Print access summary so the user can verify everything immediately
    _print_access_summary(state)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 10 — Watch & promote Rollouts
# ═════════════════════════════════════════════════════════════════════════════

def _rollout_status_summary(svc: str) -> dict:
    """Return a dict with phase, desired, ready, available, message from the Rollout."""
    # Plain kubectl -o json — the argo rollouts plugin does not support -o json
    r = subprocess.run(
        f"kubectl get rollout {svc} -n peerisland -o json",
        shell=True, capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return {"phase": "unknown", "desired": "?", "ready": "?", "available": "?", "message": r.stderr.strip()}
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"phase": "unknown", "desired": "?", "ready": "?", "available": "?", "message": "bad json"}
    st = obj.get("status", {})
    spec = obj.get("spec", {})
    return {
        "phase":     st.get("phase", "unknown"),
        "desired":   str(spec.get("replicas", "?")),
        "ready":     str(st.get("readyReplicas", "0")),
        "available": str(st.get("availableReplicas", "0")),
        "message":   st.get("message", ""),
    }


def phase10_rollouts(state: dict):
    phase_header(10, "Watch & promote Rollouts (first deploy)")
    _require_tool("kubectl-argo-rollouts")
    _ensure_kubectl_auth(state)
    _ensure_argocd_resources_patched()   # apply argocd-values.yaml (no-op if unchanged)
    _argocd_global_rpc_check_and_fix()

    # ── Ensure peerisland namespace exists (ArgoCD may not have synced yet) ───
    ns_check = subprocess.run(
        "kubectl get namespace peerisland",
        shell=True, capture_output=True, text=True,
    )
    if ns_check.returncode != 0:
        log("WARN", "namespace 'peerisland' not found — checking ArgoCD app status …")

        def _peerisland_app_status():
            r = subprocess.run(
                "kubectl get application peerisland -n argocd -o json",
                shell=True, capture_output=True, text=True,
            )
            if r.returncode != 0:
                return {}
            try:
                return json.loads(r.stdout).get("status", {})
            except Exception:
                return {}

        # Hard-refresh first — clears any cached error state without a restart
        log("INFO", "  hard-refreshing peerisland app …")
        subprocess.run(
            "kubectl annotate application peerisland -n argocd "
            "argocd.argoproj.io/refresh=hard --overwrite",
            shell=True, capture_output=True,
        )
        time.sleep(15)  # give repo-server time to attempt the build

        # Only restart app-controller for connection errors (code=Unavailable).
        # code=Unknown means kustomize/helm build failure — restart does nothing for that.
        st = _peerisland_app_status()
        conds = st.get("conditions", [])
        msgs = " ".join(c.get("message", "") for c in conds).lower()
        _controller_restarted_at = 0.0
        _is_conn_rpc = "code = unavailable" in msgs or (
            "rpc" in msgs and "code = unknown" not in msgs and "failed to generate manifest" not in msgs
        )
        if _is_conn_rpc:
            log("WARN", "  rpc error persists — restarting app-controller once …")
            subprocess.run(
                "kubectl rollout restart statefulset argocd-application-controller -n argocd",
                shell=True, capture_output=True,
            )
            subprocess.run(
                "kubectl rollout status statefulset argocd-application-controller -n argocd --timeout=120s",
                shell=True,
            )
            _controller_restarted_at = time.time()
            log("INFO", "  waiting 45s for controller to reconnect before syncing …")
            time.sleep(45)

        log("INFO", "Triggering ArgoCD sync for peerisland app …")
        subprocess.run(
            "argocd app sync peerisland --force --async",
            shell=True, capture_output=True,
        )

        log("INFO", "Waiting for 'peerisland' namespace to appear (up to 8 min) …")
        deadline_ns = time.time() + 480
        last_refresh_t = time.time()
        while time.time() < deadline_ns:
            r = subprocess.run(
                "kubectl get namespace peerisland",
                shell=True, capture_output=True, text=True,
            )
            if r.returncode == 0:
                log("OK", "namespace 'peerisland' exists — continuing")
                break
            # Every 90s: hard-refresh only (no more controller restarts — they cause loops)
            if time.time() - last_refresh_t >= 90:
                st2 = _peerisland_app_status()
                sync2 = st2.get("sync", {}).get("status", "")
                msgs2 = " ".join(c.get("message", "") for c in st2.get("conditions", [])).lower()
                _is_conn_rpc2 = "code = unavailable" in msgs2 or (
                    "rpc" in msgs2 and "code = unknown" not in msgs2 and "failed to generate manifest" not in msgs2
                )
                if _is_conn_rpc2:
                    # Only restart controller again if 5+ min since last restart
                    if time.time() - _controller_restarted_at >= 300:
                        log("WARN", "  rpc error returned — restarting controller (5 min cooldown respected) …")
                        subprocess.run(
                            "kubectl rollout restart statefulset argocd-application-controller -n argocd",
                            shell=True, capture_output=True,
                        )
                        subprocess.run(
                            "kubectl rollout status statefulset argocd-application-controller -n argocd --timeout=120s",
                            shell=True,
                        )
                        _controller_restarted_at = time.time()
                        time.sleep(45)
                    else:
                        log("INFO", "  rpc error seen but cooldown active — hard-refreshing only …")
                        subprocess.run(
                            "kubectl annotate application peerisland -n argocd "
                            "argocd.argoproj.io/refresh=hard --overwrite",
                            shell=True, capture_output=True,
                        )
                subprocess.run("argocd app sync peerisland --force --async",
                               shell=True, capture_output=True)
                last_refresh_t = time.time()
            log("INFO", f"  namespace not yet present — waiting … ({int(deadline_ns - time.time())}s left)")
            time.sleep(15)
        else:
            die("namespace 'peerisland' did not appear within 8 min — "
                "check ArgoCD app 'peerisland' for sync errors (python bootstrap.py --diagnose)")

    def _watch_rollout(svc: str, target_phase: str, timeout: int) -> dict:
        """Poll rollout until target_phase, logging every change AND every 30s."""
        log("INFO", f"Watching {svc} Rollout — waiting for phase={target_phase} …")
        last_phase = ""
        last_log_t = time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = _rollout_status_summary(svc)
            ph = info["phase"]
            msg = f"  {svc}: phase={ph}  desired={info['desired']}  ready={info['ready']}  available={info['available']}" \
                  + (f"  ({info['message']})" if info["message"] else "")
            now = time.time()
            if ph != last_phase or (now - last_log_t) >= 30:
                log("INFO", msg)
                last_phase = ph
                last_log_t = now
            if ph == target_phase:
                log("OK", f"  {svc} Rollout reached {target_phase}")
                return info
            if ph in ("Degraded", "Error"):
                log("WARN", f"  {svc} Rollout entered {ph} — {info['message']}")
            time.sleep(10)
        info = _rollout_status_summary(svc)
        die(f"{svc} Rollout did not reach {target_phase} within {timeout}s. "
            f"Last phase={info['phase']} message={info['message']!r}")

    # ── Wait for each Rollout to pause, then promote ──────────────────────────
    for svc in ("backend", "frontend"):
        _watch_rollout(svc, "Paused", timeout=300)
        log("OK", f"{svc}: promoting (first deploy — no manual traffic gate needed)")
        run(f"kubectl argo rollouts promote {svc} -n peerisland")

    # ── Confirm both reach Healthy ────────────────────────────────────────────
    for svc in ("backend", "frontend"):
        _watch_rollout(svc, "Healthy", timeout=300)

    log("OK", "Both Rollouts promoted and Healthy")
    mark_done(state, 10)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 11 — Print all URLs and credentials
# ═════════════════════════════════════════════════════════════════════════════

def phase11_summary(state: dict):
    phase_header(11, "Bootstrap complete")

    ip          = state["data"].get("appgw_ip", "<appgw-public-ip>")
    base        = f"https://{ip}"
    argocd_pw   = state["data"].get("argocd_password", "(check secret: argocd-initial-admin-secret)")
    grafana_user= state["data"].get("grafana_user",    "admin")
    grafana_pw  = state["data"].get("grafana_password", "(check Key Vault: grafana-admin-password)")

    W = 60  # box inner width

    def row(label: str, value: str = ""):
        content = f"  {label:<24}  {value}" if value else f"  {label}"
        pad = W - len(content)
        return f"║{content}{' ' * max(pad, 0)}║"

    lines = [
        f"\n{GREEN}{BOLD}╔{'═' * W}╗",
        f"║{'  PeerIsland — Bootstrap Complete':^{W}}║",
        f"╠{'═' * W}╣",
        row("App",             base),
        row("Grafana",         f"{base}/grafana"),
        row("ArgoCD",          f"{base}/argocd"),
        row("Argo Rollouts",   "kubectl argo rollouts dashboard"),
        f"╠{'═' * W}╣",
        row("ArgoCD login",    f"admin  /  {argocd_pw}"),
        row("Grafana login",   f"{grafana_user}  /  {grafana_pw}"),
        f"╠{'═' * W}╣",
        row("AKS cluster",     state["data"].get("aks_cluster_name", "")),
        row("ACR",             state["data"].get("acr_login_server", "")),
        row("Key Vault",       state["data"].get("kv_name", "")),
        f"╠{'═' * W}╣",
        row("Bootstrap log",   "bootstrap.log"),
        row("Summary file",    "bootstrap-summary.txt  (keep private)"),
        f"╚{'═' * W}╝{RESET}",
    ]
    print("\n".join(lines))

    # Next-deploy instructions
    print(f"""
{bold('Next deploy (after the first):')}
  1. Push code to main → GitHub Actions builds & bumps the image tag
  2. ArgoCD detects the tag change → syncs → Rollout controller creates green RS
  3. Both Rollouts pause — green is live on preview service
  4. Verify at {base}/grafana  or  kubectl argo rollouts get rollout backend -n peerisland -w
  5. {cyan('kubectl argo rollouts promote backend  -n peerisland')}
     {cyan('kubectl argo rollouts promote frontend -n peerisland')}

{bold('If you need to abort a bad deploy:')}
  {cyan('kubectl argo rollouts abort backend  -n peerisland')}
  {cyan('kubectl argo rollouts abort frontend -n peerisland')}
  {yellow('git revert HEAD && git push')}  # stop ArgoCD redeploying the bad tag
""")

    # Save a plain-text summary (marked private — gitignored)
    summary = REPO_ROOT / "bootstrap-summary.txt"
    summary.write_text(
        f"PeerIsland Bootstrap Summary\n"
        f"Generated: {datetime.now().isoformat()}\n"
        f"{'─' * 50}\n"
        f"App URL:        {base}\n"
        f"Grafana:        {base}/grafana\n"
        f"ArgoCD:         {base}/argocd\n"
        f"\n"
        f"ArgoCD creds:   admin / {argocd_pw}\n"
        f"Grafana creds:  {grafana_user} / {grafana_pw}\n"
        f"\n"
        f"AKS cluster:    {state['data'].get('aks_cluster_name', '')}\n"
        f"ACR:            {state['data'].get('acr_login_server', '')}\n"
        f"Key Vault:      {state['data'].get('kv_name', '')}\n",
        encoding="utf-8",
    )
    log("OK", f"Summary written to {summary}  (keep it private — it contains credentials)")
    mark_done(state, 11)


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

PHASES = {
    1:  ("Pre-flight checks",                   phase1_preflight),
    2:  ("Bootstrap TF remote state",           phase2_bootstrap_tfstate),
    3:  ("Set GitHub Actions secrets",          phase3_github_secrets),
    4:  ("Provision Azure infra (Terraform)",   phase4_terraform),
    5:  ("Configure kubectl",                   phase5_kubectl),
    6:  ("Seed Key Vault secrets",              phase6_keyvault),
    7:  ("Install ArgoCD + App of Apps",        phase7_argocd),
    8:  ("Wait for ArgoCD sync waves",          phase8_wait_argocd),
    9:  ("Trigger first GitHub Actions build",  phase9_first_build),
    10: ("Watch & promote Rollouts",            phase10_rollouts),
    11: ("Print URLs and credentials",          phase11_summary),
}


def list_phases():
    print(f"\n{bold('Phases:')}")
    for n, (title, _) in PHASES.items():
        print(f"  {cyan(str(n)):>4}  {title}")
    print()


def _ensure_kubectl_auth(state: dict):
    """
    Ensure kubelogin is installed and kubectl is authenticated against AKS.

    Called at Phase 8 start (and from --diagnose mode) so every kubectl call
    in the phase works even when the session was opened in a fresh shell that
    never ran Phase 5.
    """
    _ensure_tools_on_path()

    # Ensure kubelogin lives in _TOOLS_DIR so any terminal with _TOOLS_DIR on
    # PATH can authenticate — not just the bootstrap.py process.
    kl_exe = "kubelogin.exe" if _IS_WIN else "kubelogin"
    kl_in_tools = (_TOOLS_DIR / kl_exe).exists()
    if not kl_in_tools:
        # Check if it exists elsewhere (e.g. ~/.azure-kubelogin from az aks install-cli)
        elsewhere = next(
            (Path(p) / kl_exe
             for p in os.environ.get("PATH", "").split(os.pathsep)
             if (Path(p) / kl_exe).exists()),
            None,
        )
        if elsewhere:
            # Copy into _TOOLS_DIR so it's in the canonical location
            import shutil
            _TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(elsewhere), str(_TOOLS_DIR / kl_exe))
            log("OK", f"kubelogin copied to tools dir from {elsewhere.parent}")
        else:
            log("INFO", "kubelogin not found — installing …")
            _auto_install("kubelogin")
            _ensure_tools_on_path()
    else:
        log("OK", "kubelogin found in PATH")

    # Refresh kubeconfig from AKS
    rg      = state["data"].get("resource_group", "peerisland-prod-rg")
    cluster = state["data"].get("aks_cluster_name", "peerisland-prod-aks")
    r = subprocess.run(
        f"az aks get-credentials -g {rg} -n {cluster} --overwrite-existing",
        shell=True, capture_output=True, text=True,
    )
    if r.returncode != 0:
        log("WARN", f"az aks get-credentials failed: {(r.stderr or r.stdout)[:200]}")
    else:
        log("OK", "kubeconfig refreshed from AKS")

    # Convert kubeconfig for AAD workload-identity / kubelogin
    r2 = subprocess.run(
        "kubelogin convert-kubeconfig -l azurecli",
        shell=True, capture_output=True, text=True,
    )
    if r2.returncode != 0:
        log("WARN", f"kubelogin convert-kubeconfig failed: {(r2.stderr or r2.stdout)[:200]}")
    else:
        log("OK", "kubelogin auth conversion done")


def _ensure_tools_on_path():
    """Inject all known tool directories into PATH for the current process.

    Covers tools installed in earlier sessions or by external commands (az aks
    install-cli, pip, our own auto-installer) whose PATH entries only land in the
    Windows registry and don't reach the current process without a new shell.
    """
    extra_dirs: list[Path] = [
        _TOOLS_DIR,                                   # our own auto-installer
        Path(sys.executable).parent / "Scripts",      # pip-installed azure-cli
        Path.home() / ".azure-kubelogin",             # az aks install-cli → kubelogin
        Path.home() / ".azure-kubectl",               # az aks install-cli → kubectl
    ]
    # Also scan ~/.azure-kubelogin subdirs in case the binary is nested (e.g. bin/windows_amd64/)
    kl_root = Path.home() / ".azure-kubelogin"
    if kl_root.exists():
        for sub in kl_root.rglob("kubelogin.exe" if _IS_WIN else "kubelogin"):
            extra_dirs.append(sub.parent)

    cur_path = os.environ.get("PATH", "").lower()
    for d in extra_dirs:
        if d.exists():
            s = str(d)
            if s.lower() not in cur_path:
                os.environ["PATH"] = s + os.pathsep + os.environ["PATH"]
                cur_path = os.environ["PATH"].lower()
                if _IS_WIN:
                    try:
                        import winreg
                        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS)
                        cur, _ = winreg.QueryValueEx(key, "PATH")
                        if s.lower() not in cur.lower():
                            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, f"{s};{cur}")
                        winreg.CloseKey(key)
                    except Exception:
                        pass


def main():
    global DRY_RUN
    # Force UTF-8 everywhere — Windows defaults to cp1252 which crashes on
    # kubectl/argocd output that contains box-drawing or emoji characters.
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    _ensure_tools_on_path()

    parser = argparse.ArgumentParser(
        description="PeerIsland end-to-end bootstrap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--from",    dest="start_from", metavar="N", type=int, default=1,
                        help="Start from phase N (skip phases < N)")
    parser.add_argument("--only",    dest="only",       metavar="N", type=int, default=None,
                        help="Run only phase N")
    parser.add_argument("--dry-run", dest="dry_run",    action="store_true",
                        help="Print commands without executing them")
    parser.add_argument("--reset",   dest="reset",      action="store_true",
                        help="Clear saved state and start over from phase 1")
    parser.add_argument("--list",     dest="list_phases", action="store_true",
                        help="List all phases and exit")
    parser.add_argument("--diagnose", dest="diagnose",    action="store_true",
                        help="Show ArgoCD app status table (read-only). Fixing is handled by the phases.")
    args = parser.parse_args()

    if args.list_phases:
        list_phases()
        return

    if args.diagnose:
        state = load_state()
        _ensure_tools_on_path()
        _ensure_kubectl_auth(state)
        _print_argocd_status_table("ArgoCD Apps")
        return

    DRY_RUN = args.dry_run
    if DRY_RUN:
        print(f"\n{yellow('  DRY RUN — commands will be printed, not executed')}\n")

    state = load_state()
    if args.reset:
        state = {"completed": [], "data": {}}
        save_state(state)
        log("INFO", "State cleared — starting fresh")

    log("INFO", f"Log  → {LOG_FILE}")
    log("INFO", f"State→ {STATE_FILE}")

    to_run = [args.only] if args.only else list(PHASES.keys())

    for n in to_run:
        if n not in PHASES:
            die(f"Unknown phase: {n}")
        if not should_run(state, n, args.start_from):
            # Phase 4 already done: still check for terraform drift and auto-apply
            if n == 4:
                _terraform_drift_apply(state)
            continue
        _, fn = PHASES[n]
        try:
            fn(state)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            print(f"\n{yellow('  Interrupted — progress saved. Re-run to continue.')}")
            sys.exit(1)
        except Exception as exc:
            die(f"Phase {n} failed: {exc}")


if __name__ == "__main__":
    main()
