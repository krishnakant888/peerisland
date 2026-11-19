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

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED   = "\033[31m"; GREEN  = "\033[32m"; YELLOW = "\033[33m"
BLUE  = "\033[34m"; CYAN   = "\033[36m"; WHITE  = "\033[37m"

def _c(codes, text): return f"{codes}{text}{RESET}"
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
    "terraform": "1.9.8",
    "argocd":    "v2.12.0",
    "kustomize": "v5.4.3",
    "kubectl":   "",          # fetched dynamically from dl.k8s.io/release/stable.txt
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
_AUTO_INSTALLABLE = {"terraform", "az", "kubectl", "kubelogin", "helm", "argocd", "kustomize"}

# Track which tools have already been verified this session to avoid re-checking
_verified_tools: set[str] = set()


_TOOL_PROBES = {
    "az":        "az version",
    "kubectl":   "kubectl version --client",
    "kubelogin": "kubelogin --version",
    "helm":      "helm version",
    "argocd":    "argocd version --client",
    "kustomize": "kustomize version",
    "terraform": "terraform --version",
    "gh":        "gh --version",
    "git":       "git --version",
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

    client_id = tf("cicd_client_id")
    if not client_id:
        # Terraform hasn't run yet — look up by display name
        client_id = capture(
            "az ad sp list --display-name peerisland-prod-cicd --query [0].appId -o tsv",
            check=False,
        )

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

def phase4_terraform(state: dict):
    phase_header(4, "Provision Azure infrastructure (Terraform)")
    tf_dir = REPO_ROOT / "terraform"

    run("terraform init -upgrade", cwd=tf_dir)
    run("terraform validate",      cwd=tf_dir)

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
            log("WARN", f"Apply failed on attempt {attempt} — tainted resources will be replaced on retry …")
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

    # Apply App of Apps
    run("kubectl apply -f argocd/app-of-apps.yaml")
    log("OK", "App of Apps applied — sync waves starting")

    mark_done(state, 7)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 8 — Wait for ArgoCD sync waves
# ═════════════════════════════════════════════════════════════════════════════

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
        log("WARN", "User node pool has 0 nodes — scaling to 1 so workload pods can schedule …")
        subprocess.run(
            f"az aks nodepool scale -g {rg} --cluster-name {cluster} "
            f"--name user --node-count 1",
            shell=True, check=True, encoding="utf-8", errors="replace",
        )
        log("OK", "User node pool scaled to 1 — waiting for node to be Ready …")
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

    def all_healthy():
        r = subprocess.run(
            "kubectl get applications -n argocd -o json",
            shell=True, capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False
        apps   = json.loads(r.stdout).get("items", [])
        status = {a["metadata"]["name"]: a.get("status", {}) for a in apps}

        all_ok = True
        print()  # newline before the status table
        for name in expected:
            s      = status.get(name, {})
            sync   = s.get("sync",   {}).get("status", "Unknown")
            health = s.get("health", {}).get("status", "Unknown")
            ok     = sync == "Synced" and health in ("Healthy", "Progressing")
            icon   = green("✓") if ok else yellow("…")
            print(f"    {icon}  {name:40}  {dim(sync + '/' + health)}")
            if not ok:
                all_ok = False
        return all_ok

    wait_for(all_healthy, "All ArgoCD apps Synced/Healthy", timeout=600, interval=20)
    mark_done(state, 8)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 9 — Trigger first build (GitHub Actions)
# ═════════════════════════════════════════════════════════════════════════════

def phase9_first_build(state: dict):
    phase_header(9, "Trigger first build via GitHub Actions")
    org       = state["data"].get("github_org",  "")
    repo_name = state["data"].get("github_repo", "")
    slug      = f"{org}/{repo_name}"

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


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 10 — Watch & promote Rollouts
# ═════════════════════════════════════════════════════════════════════════════

def phase10_rollouts(state: dict):
    phase_header(10, "Watch & promote Rollouts (first deploy)")

    for svc in ("backend", "frontend"):
        def paused(s=svc):
            r = subprocess.run(
                f"kubectl argo rollouts get rollout {s} -n peerisland -o json",
                shell=True, capture_output=True, text=True,
            )
            if r.returncode != 0:
                return False
            ph = json.loads(r.stdout).get("status", {}).get("phase", "")
            print(f"  {dim(f'  {s} phase: {ph}')}", end="\r", flush=True)
            return ph == "Paused"

        wait_for(paused, f"{svc} Rollout paused (green ReplicaSet ready)", timeout=300, interval=10)
        print()

        log("OK",   f"{svc}: promoting on first deploy (no manual traffic verification needed)")
        run(f"kubectl argo rollouts promote {svc} -n peerisland")

    # Confirm both reach Healthy
    for svc in ("backend", "frontend"):
        def healthy(s=svc):
            r = subprocess.run(
                f"kubectl argo rollouts get rollout {s} -n peerisland -o json",
                shell=True, capture_output=True, text=True,
            )
            if r.returncode != 0:
                return False
            ph = json.loads(r.stdout).get("status", {}).get("phase", "")
            print(f"  {dim(f'  {s} phase: {ph}')}", end="\r", flush=True)
            return ph == "Healthy"

        wait_for(healthy, f"{svc} Rollout Healthy", timeout=120, interval=5)
        print()

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
    parser.add_argument("--list",    dest="list_phases", action="store_true",
                        help="List all phases and exit")
    args = parser.parse_args()

    if args.list_phases:
        list_phases()
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
