#!/usr/bin/env python3
"""
account-sync — keep every Claude Code account on every machine identical.

It works in two phases:

  harvest  Read every account's config (settings.json, skills, agents, commands,
           rules, output styles, CLAUDE.md, user-scope MCP servers, plugins and
           marketplaces) and fold it into one UNION stored in a "hub" folder
           (ideally a private git repo, so several machines can share it).

  apply    Push the hub back into every account: copy files, merge settings,
           add missing MCP servers, add marketplaces and install plugins with the
           official `claude` CLI.

  sync     harvest + apply (+ git pull/push). This is what the hook and the
           scheduler run.

Account-level things that live on claude.ai (connectors, claude.ai-synced skills
and plugins) cannot be written from a local machine; account-sync inventories
them per account and reports exactly what each account is missing.

Python 3.8+, standard library only. Run `account_sync.py --help`.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.0.0"
HOME = Path.home()
STATE_DIR = Path(os.environ.get("ACCOUNT_SYNC_HOME", str(HOME / ".account-sync"))).expanduser()
CONFIG_PATH = STATE_DIR / "config.json"
SECRETS_PATH = STATE_DIR / "secrets.json"      # never inside the hub / git repo
STATE_PATH = STATE_DIR / "state.json"
BACKUP_DIR = STATE_DIR / "backups"
LOCK_PATH = STATE_DIR / "sync.lock"
LOG_PATH = STATE_DIR / "sync.log"
HOSTNAME = os.environ.get("ACCOUNT_SYNC_HOSTNAME") or socket.gethostname().split(".")[0] or "host"
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
PLATFORM = os.environ.get("ACCOUNT_SYNC_PLATFORM") or ("windows" if IS_WINDOWS else ("macos" if IS_MAC else "linux"))
TOOL = "account-sync"
# settings.json keys that hold shell commands or OS-specific choices. They are kept per OS in the hub
# (hub/settings.<os>.json), so a Mac status-line script never lands on a Windows PC and vice versa.
OS_SPECIFIC_KEYS = ("hooks", "statusLine", "subagentStatusLine", "fileSuggestion", "processWrapper", "defaultShell")

# What gets copied between accounts (relative to each account's config dir).
DEFAULT_SYNC_ITEMS = [
    "CLAUDE.md", "keybindings.json",
    "skills", "commands", "agents", "rules", "output-styles", "workflows", "themes",
    "hooks", "scripts",
    "*.sh", "*.py", "*.js", "*.mjs", "*.ps1",          # top-level helper scripts (status line, hooks)
]
# Path components never copied.
EXCLUDED_COMPONENTS = {".trash", "__pycache__", "node_modules", ".git", ".DS_Store", "Thumbs.db"}
# Top-level names that are account state / credentials and must never be touched.
NEVER_TOUCH = {
    ".credentials.json", ".claude.json", "settings.json", "settings.local.json", "projects",
    "history.jsonl", "plugins", "file-history", "todos", "statsig", "logs", "shell-snapshots",
    "session-env", "sessions", "tasks", "debug", "plans", "paste-cache", "image-cache", "uploads",
    "backups", "cache", "usage-data", "feedback", "feedback-bundles", "jobs", "daemon",
    "agent-memory", "stats-cache.json", "remote-settings.json", "policy-limits.json", "ide",
    "dev-mods", "account_sync.py",
}
NEVER_TOUCH_CF = {n.casefold() for n in NEVER_TOUCH}
EXCLUDED_COMPONENTS_CF = {n.casefold() for n in EXCLUDED_COMPONENTS}
# settings.json keys that are tied to a specific account's sign-in; never synced.
DEFAULT_SETTINGS_EXCLUDE = [
    "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh",
    "otelHeadersHelper", "forceLoginMethod", "forceLoginOrgUUID", "forceLoginGatewayUrl",
]
# env entries in settings.json tied to a specific account's sign-in; never synced.
DEFAULT_ENV_EXCLUDE = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
]
ORDER_SENSITIVE_LISTS = {"fallbackModel"}
HOOK_MARKER = "account_sync.py"                        # our own SessionStart hook; machine-local
LEGACY_HOOK_MARKER = "claude_sync.py"                  # same hook from before the rename
LEGACY_STATE = HOME / ".claude-sync"                   # state folder from before the rename


def is_our_hook(h):
    cmd = str((h or {}).get("command", ""))
    return HOOK_MARKER in cmd or LEGACY_HOOK_MARKER in cmd

SECRET_KEY_RE = re.compile(r"(token|secret|passw|api[_-]?key|apikey|access[_-]?key|private|credential|auth|cookie|session)", re.I)
SECRET_VAL_RE = re.compile(r"^(sk-|sk_|ghp_|gho_|ghs_|github_pat_|xox[abpr]-|glpat-|AKIA|ASIA|AIza|ya29\.|eyJ[A-Za-z0-9_-]{10,}\.)")
SECRET_FLAG_RE = re.compile(r"^--?[\w-]*(token|secret|passw|api[_-]?key|apikey|key|auth)[\w-]*$", re.I)
PLACEHOLDER_RE = re.compile(r"\$\{(CSYNC_[A-Z0-9_]+)\}")


# ───────────────────────────── small utilities ─────────────────────────────

class Log:
    quiet = False
    lines: list = []

    @classmethod
    def info(cls, msg=""):
        cls.lines.append(msg)
        if not cls.quiet and sys.stdout is not None:     # pythonw (Windows scheduled task) has no console
            print(msg)

    @classmethod
    def warn(cls, msg):
        cls.info("  ! " + msg)


def now_stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ─────────────────────────────── platform helpers ───────────────────────────────

def py_cmd():
    """How to start Python on this OS, for commands we print."""
    if IS_WINDOWS:
        return "py -3" if shutil.which("py") else "python"
    return "python3"


def shown(path):
    """A path as the user should type it in this OS's terminal."""
    s = str(path)
    if IS_WINDOWS:
        return f'"{s}"'
    home = str(HOME)
    if s == home or s.startswith(home + "/"):
        rel = s[len(home):]
        return f'"$HOME{rel}"' if " " in rel else f"~{rel}"
    return shlex.quote(s)


def run_powershell(script, env_extra=None):
    env = os.environ.copy()
    env.update(env_extra or {})
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, env=env)


def install_command(name, script):
    """Put a `name` command on PATH that runs `script` with this Python. Returns a note for the user."""
    bindir = HOME / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    on_path = any(Path(p).resolve() == bindir.resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    if IS_WINDOWS:
        (bindir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        if not on_path:
            r = run_powershell(
                "$p=[Environment]::GetEnvironmentVariable('Path','User'); "
                "if (-not (($p -split ';') -contains $env:KS_BIN)) { "
                "[Environment]::SetEnvironmentVariable('Path', (($p.TrimEnd(';') + ';' + $env:KS_BIN).TrimStart(';')), 'User') }",
                {"KS_BIN": str(bindir)})
            if r.returncode != 0:
                return f"Add {bindir} to your PATH to use the `{name}` command."
            return f"Added the `{name}` command. Open a new terminal window to use it."
        return f"Added the `{name}` command."
    shim = bindir / name
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    os.chmod(shim, 0o755)
    if on_path:
        return f"Added the `{name}` command."
    shell = os.path.basename(os.environ.get("SHELL", ""))
    if shell == "fish":
        rc, line = HOME / ".config" / "fish" / "config.fish", "fish_add_path $HOME/.local/bin"
    else:
        rc = {"zsh": HOME / ".zshrc", "bash": HOME / (".bash_profile" if IS_MAC else ".bashrc")}.get(shell, HOME / ".profile")
        line = 'export PATH="$HOME/.local/bin:$PATH"'
    try:
        text = rc.read_text(encoding="utf-8") if rc.exists() else ""
        if line not in text:
            rc.parent.mkdir(parents=True, exist_ok=True)
            with open(rc, "a", encoding="utf-8") as f:
                f.write(f"\n# added by {name}\n{line}\n")
    except OSError:
        return f"Add ~/.local/bin to your PATH to use the `{name}` command."
    return f"Added the `{name}` command. Open a new terminal window to use it."


def pythonw():
    """On Windows, the console-less Python for background/scheduled runs."""
    p = Path(sys.executable).with_name("pythonw.exe")
    return str(p) if IS_WINDOWS and p.exists() else sys.executable


def split_os_keys(settings):
    shared = {k: v for k, v in settings.items() if k not in OS_SPECIFIC_KEYS}
    per_os = {k: v for k, v in settings.items() if k in OS_SPECIFIC_KEYS}
    return shared, per_os


def load_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Cannot parse {path}: {e}. Fix or move the file, then retry.")


def write_json_atomic(path: Path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".csync-tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    elif path.exists():
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
    os.replace(tmp, path)


def canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_path(p: Path) -> str:
    h = hashlib.sha256()
    if p.is_file():
        h.update(p.read_bytes())
        return h.hexdigest()
    for f in sorted(iter_files(p)):
        h.update(f.relative_to(p).as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_COMPONENTS]
        for fn in filenames:
            if fn in EXCLUDED_COMPONENTS or fn.endswith(".pyc"):
                continue
            yield Path(dirpath) / fn


def newest_mtime(p: Path) -> float:
    if p.is_file():
        return p.stat().st_mtime
    times = [f.stat().st_mtime for f in iter_files(p)]
    return max(times) if times else p.stat().st_mtime


def copy_unit(src: Path, dst: Path):
    if dst.exists() or dst.is_symlink():
        remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*EXCLUDED_COMPONENTS, "*.pyc"))
    else:
        shutil.copy2(src, dst)
    if not IS_WINDOWS:                         # scripts committed from Windows lose their executable bit
        for f in ([dst] if dst.is_file() else iter_files(dst)):
            try:
                with open(f, "rb") as fh:
                    if fh.read(2) == b"#!":
                        os.chmod(f, os.stat(f).st_mode | 0o111)
            except OSError:
                pass


def remove_path(p: Path):
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


def backup(path: Path, stamp: str, label: str):
    """Copy a file/dir we are about to change into ~/.account-sync/backups (outside the hub)."""
    if not path.exists():
        return
    try:
        rel = path.relative_to(HOME)
    except ValueError:
        rel = Path(path.as_posix().lstrip("/").replace(":", ""))
    dst = BACKUP_DIR / stamp / label / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(path, dst)


# ─────────────────────────────── config / state ───────────────────────────────

def default_config():
    return {
        "version": 1,
        "hub": str(STATE_DIR / "hub"),
        "accounts": [],
        "primary": None,
        "sync_items": list(DEFAULT_SYNC_ITEMS),
        "settings_exclude": list(DEFAULT_SETTINGS_EXCLUDE),
        "env_exclude": list(DEFAULT_ENV_EXCLUDE),
        "claude_bin": None,
        "inventory_connectors": True,
        "min_interval_minutes": 30,
    }


def load_config(required=True):
    cfg = load_json(CONFIG_PATH)
    if cfg is None:
        if required:
            raise SystemExit(f"account-sync is not set up yet. Run:  {py_cmd()} account_sync.py init")
        return None
    base = default_config()
    base.update(cfg)
    return base


def save_config(cfg):
    write_json_atomic(CONFIG_PATH, cfg)


# ─────────────────────────────── accounts ───────────────────────────────

def account_record(config_dir: Path, name=None):
    config_dir = Path(os.path.abspath(os.path.expanduser(str(config_dir))))
    is_default = config_dir == HOME / ".claude"
    gc = HOME / ".claude.json" if is_default else config_dir / ".claude.json"
    if name is None:
        name = "default" if is_default else re.sub(r"^\.?claude[-_.]?", "", config_dir.name) or config_dir.name
    return {"name": name, "config_dir": str(config_dir), "global_config": str(gc), "default": is_default}


def looks_like_config_dir(p: Path) -> bool:
    markers = ["settings.json", ".claude.json", ".credentials.json", "projects", "plugins",
               "skills", "CLAUDE.md", "history.jsonl", "agents", "commands"]
    return p.is_dir() and any((p / m).exists() for m in markers)


def discover_accounts():
    """Find every Claude Code config directory for this user on this machine."""
    cands = [HOME / ".claude"]
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        cands.append(Path(env).expanduser())
    try:
        for p in HOME.iterdir():
            if p.is_dir() and p.name.lower().startswith(".claude") and p.name not in (".claude", ".account-sync", LEGACY_STATE.name):
                cands.append(p)
    except OSError:
        pass
    # Shell profiles / aliases often hold `CLAUDE_CONFIG_DIR=~/.claude-work claude`.
    rc_files = [".zshrc", ".bashrc", ".bash_profile", ".profile", ".zprofile", ".bash_aliases",
                ".config/fish/config.fish", "Documents/PowerShell/Microsoft.PowerShell_profile.ps1",
                "Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1"]
    pat = re.compile(r"""CLAUDE_CONFIG_DIR\s*[= ]\s*["']?([^"'\s;)]+)""")
    for rc in rc_files:
        f = HOME / rc
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in pat.finditer(text):
            raw = m.group(1).replace("${HOME}", str(HOME)).replace("$HOME", str(HOME)).replace("$env:USERPROFILE", str(HOME))
            cands.append(Path(raw).expanduser())
    seen, out = set(), []
    for c in cands:
        c = Path(os.path.abspath(os.path.expanduser(str(c))))
        if c in seen:
            continue
        seen.add(c)
        if c == HOME / ".claude" or looks_like_config_dir(c):
            out.append(account_record(c))
    names = set()
    for a in out:                                   # make names unique
        base, i = a["name"], 2
        while a["name"] in names:
            a["name"] = f"{base}-{i}"
            i += 1
        names.add(a["name"])
    return out


def ordered_accounts(cfg, prefer=None):
    accts = list(cfg["accounts"])
    lead = prefer or cfg.get("primary")
    if lead:
        accts.sort(key=lambda a: 0 if a["name"] == lead else 1)
    return accts


# ──────────────────────── path tokens & secrets ────────────────────────

def _path_forms(p: Path):
    forms = {str(p), p.as_posix()}
    try:
        rel = p.relative_to(HOME).as_posix()
        forms |= {f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"}
    except ValueError:
        pass
    return sorted(forms, key=len, reverse=True)


def _sub_forms(s: str, forms, token):
    for form in forms:
        s = re.sub(re.escape(form) + r"(?=$|[\\/\s\"'`;:,)\]}])", lambda _m: token, s)
    return s


def tokenize(obj, acct):
    cdir_forms = _path_forms(Path(acct["config_dir"]))
    home_forms = sorted({str(HOME), HOME.as_posix()}, key=len, reverse=True)

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return _sub_forms(_sub_forms(x, cdir_forms, "{{CLAUDE_CONFIG_DIR}}"), home_forms, "{{HOME}}")
        return x
    return walk(obj)


def detokenize(obj, acct):
    cdir = acct["config_dir"] if IS_WINDOWS else Path(acct["config_dir"]).as_posix()
    home = str(HOME) if IS_WINDOWS else HOME.as_posix()

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return x.replace("{{CLAUDE_CONFIG_DIR}}", cdir).replace("{{HOME}}", home)
        return x
    return walk(obj)


def _var(*parts):
    return "CSYNC_" + re.sub(r"[^A-Z0-9]+", "_", "_".join(parts).upper()).strip("_")


def _is_placeholder(v: str):
    return "${" in v


def _redact_value(var, value, secrets):
    """Return the redacted form of a secret value and remember the original locally."""
    m = re.match(r"^(Bearer|Basic|Token)\s+(.+)$", value, re.I)
    if m:
        secrets[var] = m.group(2)
        return f"{m.group(1)} ${{{var}}}"
    secrets[var] = value
    return f"${{{var}}}"


def redact_mapping(mapping, prefix, secrets):
    out = {}
    for k, v in mapping.items():
        if isinstance(v, str) and v and not _is_placeholder(v) and (SECRET_KEY_RE.search(k) or SECRET_VAL_RE.search(v)):
            out[k] = _redact_value(_var(prefix, k), v, secrets)
        else:
            out[k] = v
    return out


def redact_url(url, prefix, secrets):
    def rep(m):
        key, val = m.group(1), m.group(2)
        if SECRET_KEY_RE.search(key) and not _is_placeholder(val):
            var = _var(prefix, "URL", key)
            secrets[var] = val
            return f"{key}=${{{var}}}"
        return m.group(0)
    return re.sub(r"(?<=[?&])([^=&#]+)=([^&#]*)", rep, url)


def redact_mcp(name, srv, secrets):
    srv = copy.deepcopy(srv)
    prefix = f"MCP_{name}"
    for key in ("env", "headers"):
        if isinstance(srv.get(key), dict):
            srv[key] = redact_mapping(srv[key], prefix, secrets)
    if isinstance(srv.get("url"), str):
        srv["url"] = redact_url(srv["url"], prefix, secrets)
    if isinstance(srv.get("args"), list):
        args, prev = [], ""
        for i, a in enumerate(srv["args"]):
            if isinstance(a, str) and not _is_placeholder(a):
                if "=" in a and a.startswith("-") and SECRET_FLAG_RE.match(a.split("=", 1)[0]):
                    flag, val = a.split("=", 1)
                    a = f"{flag}={_redact_value(_var(prefix, 'ARG', flag), val, secrets)}"
                elif (SECRET_FLAG_RE.match(prev) and not a.startswith("-")) or SECRET_VAL_RE.search(a):
                    a = _redact_value(_var(prefix, "ARG", str(i)), a, secrets)
            args.append(a)
            prev = a if isinstance(a, str) else ""
        srv["args"] = args
    return srv


def restore_secrets(obj, secrets, missing: set):
    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            def rep(m):
                var = m.group(1)
                if var in secrets:
                    return secrets[var]
                missing.add(var)
                return m.group(0)
            return PLACEHOLDER_RE.sub(rep, x)
        return x
    return walk(obj)


# ─────────────────────────────── settings merge ───────────────────────────────

def merge_union(base, top, path, conflicts, top_src):
    """Union merge: dicts recurse, lists union, scalars -> `top` wins (conflict recorded)."""
    if isinstance(base, dict) and isinstance(top, dict):
        out = dict(base)
        for k, v in top.items():
            p = f"{path}.{k}" if path else k
            out[k] = merge_union(out[k], v, p, conflicts, top_src) if k in out else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(top, list) and path.split(".")[-1] not in ORDER_SENSITIVE_LISTS:
        seen, out = {canon(i) for i in base}, list(base)
        for item in top:
            c = canon(item)
            if c not in seen:
                out.append(copy.deepcopy(item))
                seen.add(c)
        return out
    if canon(base) != canon(top):
        conflicts.append((path, base, top, top_src))
    return copy.deepcopy(top)


def strip_local_only(settings, cfg):
    """Remove account-specific keys and our machine-local hook before harvesting."""
    s = copy.deepcopy(settings or {})
    for k in cfg["settings_exclude"]:
        s.pop(k, None)
    if isinstance(s.get("env"), dict):
        for k in cfg["env_exclude"]:
            s["env"].pop(k, None)
        if not s["env"]:
            s.pop("env")
    hooks = s.get("hooks")
    if isinstance(hooks, dict):
        for ev in list(hooks):
            groups = []
            for g in hooks[ev] if isinstance(hooks[ev], list) else []:
                if not isinstance(g, dict):
                    continue
                inner = [h for h in g.get("hooks", []) if not is_our_hook(h)]
                if inner:
                    g = dict(g, hooks=inner)
                    groups.append(g)
            if groups:
                hooks[ev] = groups
            else:
                hooks.pop(ev)
        if not hooks:
            s.pop("hooks")
    return s


def local_only_part(settings, cfg):
    """The subset of an account's settings that must survive an apply untouched."""
    s = settings or {}
    keep = {k: s[k] for k in cfg["settings_exclude"] if k in s}
    env = {k: v for k, v in (s.get("env") or {}).items() if k in cfg["env_exclude"]}
    if env:
        keep["env"] = env
    hooks = {}
    for ev, groups in (s.get("hooks") or {}).items():
        mine = [g for g in groups if any(is_our_hook(h) for h in g.get("hooks", []))]
        if mine:
            hooks[ev] = mine
    if hooks:
        keep["hooks"] = hooks
    return keep


# ─────────────────────────────── claude CLI ───────────────────────────────

class ClaudeCLI:
    def __init__(self, cfg):
        self.exe = cfg.get("claude_bin") or shutil.which("claude")
        # An npm-installed claude on Windows is a .cmd script; cmd.exe mangles JSON arguments,
        # so MCP server JSON is written straight into the config file instead.
        self.is_shim = bool(self.exe) and IS_WINDOWS and self.exe.lower().endswith((".cmd", ".bat"))

    @property
    def ok(self):
        return bool(self.exe)

    def run(self, acct, args, timeout=300):
        env = os.environ.copy()
        if acct.get("default"):
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = acct["config_dir"]
        try:
            r = subprocess.run([self.exe] + args, env=env, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
            return r.returncode, r.stdout, r.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            return 999, "", str(e)

    def json(self, acct, args, timeout=300):
        code, out, err = self.run(acct, args, timeout)
        if code != 0:
            return None
        for chunk in (out, out.strip().splitlines()[-1] if out.strip() else ""):
            try:
                return json.loads(chunk)
            except (json.JSONDecodeError, IndexError):
                continue
        return None


def marketplace_spec(m):
    src = m.get("source")
    if src == "claudeai":
        return {"type": "claudeai", "name": m.get("name")}
    spec = {"github": m.get("repo"), "git": m.get("url"), "url": m.get("url"),
            "directory": m.get("path"), "file": m.get("path")}.get(src)
    if not spec:
        return None
    if m.get("ref") and src in ("github", "git"):
        spec += "#" + m["ref"]
    return {"type": src, "source": spec}


def list_account_plugins(cli, acct):
    """Return (marketplaces{name:spec}, plugins{id:enabled}, synced[list]) or None if CLI unusable."""
    ml = cli.json(acct, ["plugin", "marketplace", "list", "--json"])
    pl = cli.json(acct, ["plugin", "list", "--json"])
    if ml is None and pl is None:
        return None
    markets = {}
    for m in ml or []:
        spec = marketplace_spec(m)
        if spec and m.get("name"):
            markets[m["name"]] = spec
    plugins, synced = {}, []
    for p in (pl or []) if isinstance(pl, list) else (pl or {}).get("installed", []):
        pid, scope = p.get("id", ""), p.get("scope")
        if pid.endswith("@synced"):
            synced.append(pid)
        elif scope == "user" and "@" in pid and not pid.endswith(("@inline", "@skills-dir")):
            plugins[pid] = bool(p.get("enabled", True))
    return markets, plugins, synced


def list_account_connectors(cli, acct):
    code, out, err = cli.run(acct, ["mcp", "list"], timeout=240)
    if code == 999:
        return None
    names = set()
    for line in out.splitlines():
        m = re.match(r"^\s*(claude\.ai [^:]+?):", line)
        if m:
            names.add(m.group(1).strip())
    return sorted(names)


# ─────────────────────────────── git ───────────────────────────────

def git(hub: Path, *args, check=False):
    try:
        r = subprocess.run(["git", "-C", str(hub)] + list(args), capture_output=True, text=True)
    except OSError:
        return 999, "", "git not found"
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{r.stderr}")
    return r.returncode, r.stdout, r.stderr


def ensure_gitattributes(hub):
    """Stop git (notably on Windows, core.autocrlf) from rewriting line endings in synced files;
    otherwise the same skill would look different on a PC and a Mac and show up as a conflict."""
    ga = hub / ".gitattributes"
    if not ga.exists() or "* -text" not in ga.read_text(encoding="utf-8", errors="ignore"):
        ga.write_text("* -text\n", encoding="utf-8")


def hub_is_git(hub):
    return (hub / ".git").exists()


def hub_has_remote(hub):
    code, out, _ = git(hub, "remote")
    return code == 0 and out.strip() != ""


def git_pull(hub, no_git):
    if no_git or not hub_is_git(hub) or not hub_has_remote(hub):
        return
    code, _, err = git(hub, "pull", "--rebase", "--autostash", "-q")
    if code != 0 and "couldn't find remote ref" not in err and "no tracking information" not in err:
        Log.warn(f"git pull failed (continuing with local hub): {err.strip()[:300]}")


def git_commit_push(hub, no_git, message):
    if no_git or not hub_is_git(hub):
        return
    git(hub, "add", "-A")
    _, out, _ = git(hub, "status", "--porcelain")
    if out.strip():
        git(hub, "-c", "user.name=account-sync", "-c", "user.email=account-sync@localhost", "commit", "-q", "-m", message)
        Log.info(f"Hub: committed changes ({message}).")
    if hub_has_remote(hub):
        code, _, err = git(hub, "push", "-q", "-u", "origin", "HEAD")
        if code != 0:
            Log.warn(f"git push failed: {err.strip()[:300]}")


# ─────────────────────────────── file units ───────────────────────────────

def is_excluded_rel(rel: str) -> bool:
    parts = rel.split("/")
    if parts[0].casefold() in NEVER_TOUCH_CF:
        return True
    if any(p.casefold() in EXCLUDED_COMPONENTS_CF for p in parts):
        return True
    if len(parts) >= 2 and parts[0] == "skills" and parts[1].lower() == "synced":
        return True                                   # claude.ai-synced skills: account-level, not ours
    return False


def collect_units(root: Path, items):
    """Map rel-path -> absolute path for each sync unit under root.

    A unit is a top-level file (CLAUDE.md), or one direct child of a synced folder
    (skills/<name>/, agents/<file>.md), so a skill is always copied as a whole."""
    units = {}
    if not root.exists():
        return units
    for item in items:
        if any(ch in item for ch in "*?["):
            for p in root.glob(item):
                if p.is_file() and not is_excluded_rel(p.name):
                    units[p.name] = p
            continue
        p = root / item
        if p.is_file() and not is_excluded_rel(item):
            units[item] = p
        elif p.is_dir():
            for child in p.iterdir():
                rel = f"{item}/{child.name}"
                if not is_excluded_rel(rel):
                    units[rel] = child
    return units


# ─────────────────────────────── harvest ───────────────────────────────

def harvest(cfg, args):
    hub = Path(cfg["hub"])
    hub.mkdir(parents=True, exist_ok=True)
    git_pull(hub, args.no_git)
    secrets = load_json(SECRETS_PATH, {})
    manifest = load_json(hub / "manifest.json", {}) or {}
    before = canon({k: v for k, v in manifest.items() if k != "updated"})
    manifest.setdefault("version", 1)
    for k in ("mcpServers", "marketplaces", "plugins", "accounts"):
        manifest.setdefault(k, {})
    prefer = getattr(args, "prefer", None)
    accts = ordered_accounts(cfg, prefer)
    cli = ClaudeCLI(cfg)
    files_dir, conflicts_dir = hub / "files", hub / "conflicts"
    Log.info(f"Harvesting from {len(accts)} account(s) on {HOSTNAME} into {hub}")

    # 1) settings.json — newest edit wins scalar conflicts; lists & objects are unioned.
    #    Shared keys go to hub/settings.json; command-bearing keys (hooks, statusLine, …) go to
    #    hub/settings.<os>.json and are only applied to accounts on the same OS.
    hub_settings_path = hub / "settings.json"
    hub_os_path = hub / f"settings.{PLATFORM}.json"
    shared_c, os_c = [], []
    if hub_settings_path.exists():
        hub_shared, _legacy = split_os_keys(load_json(hub_settings_path, {}) or {})   # older hubs kept them shared
        shared_c.append((hub_settings_path.stat().st_mtime, "hub", hub_shared))
    if hub_os_path.exists():
        os_c.append((hub_os_path.stat().st_mtime, "hub", load_json(hub_os_path, {}) or {}))
    for a in accts:
        sp = Path(a["config_dir"]) / "settings.json"
        if sp.exists():
            s = strip_local_only(load_json(sp, {}), cfg)
            if isinstance(s.get("env"), dict):
                s["env"] = redact_mapping(s["env"], "ENV", secrets)
            mtime = float("inf") if a["name"] == prefer else sp.stat().st_mtime
            shared, per_os = split_os_keys(tokenize(s, a))
            shared_c.append((mtime, a["name"], shared))
            if per_os:
                os_c.append((mtime, a["name"], per_os))
    s_conflicts = []

    def merge_all(cands):
        out = {}
        for _, src, data in sorted(cands, key=lambda c: c[0]):
            out = merge_union(out, data, "", s_conflicts, src)
        return out

    merged = merge_all(shared_c)
    merged_os = merge_all(os_c)
    if canon(merged) != canon(load_json(hub_settings_path, None)):   # keep mtime meaningful
        write_json_atomic(hub_settings_path, merged)
    if merged_os and canon(merged_os) != canon(load_json(hub_os_path, None)):
        write_json_atomic(hub_os_path, merged_os)
    for path, old, new, src in s_conflicts:
        Log.warn(f"settings conflict at '{path}': {canon(old)[:60]} -> {canon(new)[:60]} (newest, from {src})")

    # 2) files: CLAUDE.md, skills, agents, commands, rules, output styles, ...
    all_units = {}
    for rel, p in collect_units(files_dir, cfg["sync_items"]).items():
        all_units.setdefault(rel, []).append(("hub", p))
    for a in accts:
        for rel, p in collect_units(Path(a["config_dir"]), cfg["sync_items"]).items():
            all_units.setdefault(rel, []).append((a["name"], p))
    added = changed = file_conflicts = 0
    for rel, versions in sorted(all_units.items()):
        by_hash = {}
        for src, p in versions:
            by_hash.setdefault(hash_path(p), []).append((src, p))
        if len(by_hash) == 1:
            winner_src, winner = versions[0]
            if winner_src == "hub":
                continue
        else:
            def rank(v):
                src, p = v
                return (1 if src == prefer else 0, newest_mtime(p))
            winner_src, winner = max(versions, key=rank)
            losers = {h: vs[0] for h, vs in by_hash.items() if hash_path(winner) != h}
            for src, p in losers.values():
                copy_unit(p, conflicts_dir / f"{HOSTNAME}-{src}" / rel)
            file_conflicts += 1
            Log.warn(f"'{rel}' differs between accounts; kept the newest ({winner_src}), "
                     f"other versions saved under hub/conflicts/")
        target = files_dir / rel
        if winner_src != "hub":
            existed = target.exists()
            if not existed or hash_path(target) != hash_path(winner):
                copy_unit(winner, target)
                added += 0 if existed else 1
                changed += 1 if existed else 0

    # 3) user-scope MCP servers (stored in each account's .claude.json)
    mcp_added, mcp_conf = 0, 0
    for a in accts:
        gc = load_json(Path(a["global_config"]), {}) or {}
        servers = gc.get("mcpServers") or {}
        acct_key = f"{HOSTNAME}/{a['name']}"
        manifest["accounts"].setdefault(acct_key, {})["mcp"] = sorted(servers)
        for name, srv in servers.items():
            clean = tokenize(redact_mcp(name, srv, secrets), a)
            if name not in manifest["mcpServers"]:
                manifest["mcpServers"][name] = clean
                mcp_added += 1
            elif canon(manifest["mcpServers"][name]) != canon(clean):
                if prefer and a["name"] == prefer:
                    manifest["mcpServers"][name] = clean
                else:
                    mcp_conf += 1
                    Log.warn(f"MCP server '{name}' is configured differently in {a['name']}; "
                             f"kept the hub version (use --prefer {a['name']} to take this one)")

    # 4) plugins, marketplaces, connectors (via the official CLI)
    if not cli.ok:
        Log.warn("`claude` CLI not found on PATH; plugin/connector inventory skipped "
                 "(settings.json enabledPlugins/extraKnownMarketplaces still sync). "
                 "Set \"claude_bin\" in ~/.account-sync/config.json.")
    plugins_seen = {}
    for a in accts:
        acct_key = f"{HOSTNAME}/{a['name']}"
        rec = manifest["accounts"].setdefault(acct_key, {})
        synced_skills_dir = Path(a["config_dir"]) / "skills" / "synced"
        rec["claudeai_skills"] = sorted(p.name for p in synced_skills_dir.iterdir()) if synced_skills_dir.is_dir() else []
        if not cli.ok:
            continue
        res = list_account_plugins(cli, a)
        if res is None:
            Log.warn(f"{a['name']}: could not list plugins with the CLI")
        else:
            markets, plugins, synced = res
            for mname, spec in markets.items():
                spec = tokenize(spec, a)
                manifest["marketplaces"].setdefault(mname, spec)
            for pid, enabled in plugins.items():
                plugins_seen[pid] = plugins_seen.get(pid, False) or enabled
            rec["plugins"] = sorted(plugins)
            rec["claudeai_plugins"] = synced
        if cfg.get("inventory_connectors", True) and not getattr(args, "skip_connectors", False):
            conns = list_account_connectors(cli, a)
            if conns is not None:
                rec["connectors"] = conns
    for pid, en in (merged.get("enabledPlugins") or {}).items():
        plugins_seen[pid] = plugins_seen.get(pid, False) or bool(en)
    for mname, decl in (merged.get("extraKnownMarketplaces") or {}).items():
        src = (decl or {}).get("source") or {}
        spec = src.get("repo") or src.get("url") or src.get("path")
        if spec and mname not in manifest["marketplaces"]:
            manifest["marketplaces"][mname] = {"type": src.get("source", "github"), "source": spec}
    for pid, en in plugins_seen.items():
        prev = manifest["plugins"].get(pid, {}).get("enabled", False)
        manifest["plugins"][pid] = {"enabled": prev or en}

    if canon({k: v for k, v in manifest.items() if k != "updated"}) != before:
        manifest["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        write_json_atomic(hub / "manifest.json", manifest)
    write_json_atomic(SECRETS_PATH, secrets, mode=0o600)
    gi = hub / ".gitignore"
    if not gi.exists():
        gi.write_text(".DS_Store\n*.csync-tmp-*\nLAST_RUN.md\n", encoding="utf-8")
    ensure_gitattributes(hub)
    Log.info(f"Harvest done: {len(all_units)} file units ({added} new, {changed} updated, {file_conflicts} conflicts), "
             f"{len(manifest['mcpServers'])} MCP servers ({mcp_added} new), "
             f"{len(manifest['marketplaces'])} marketplaces, {len(manifest['plugins'])} plugins.")
    return manifest


# ─────────────────────────────── apply ───────────────────────────────

def apply(cfg, args, manifest=None):
    hub = Path(cfg["hub"])
    if manifest is None:
        git_pull(hub, args.no_git)
        manifest = load_json(hub / "manifest.json")
        if manifest is None:
            raise SystemExit("The hub is empty. Run `harvest` first.")
    dry = args.dry_run
    force = getattr(args, "force", False)
    prune = getattr(args, "prune", False)
    secrets = load_json(SECRETS_PATH, {})
    hub_shared, _ = split_os_keys(load_json(hub / "settings.json", {}) or {})
    hub_settings = merge_union(hub_shared, load_json(hub / f"settings.{PLATFORM}.json", {}) or {}, "", [], "os")
    hub_units = collect_units(hub / "files", cfg["sync_items"])
    cli = ClaudeCLI(cfg)
    stamp = now_stamp()
    only = set(args.account or [])
    missing_vars = set()
    summary = []
    tag = "[dry-run] " if dry else ""

    for a in cfg["accounts"]:
        if only and a["name"] not in only:
            continue
        cdir = Path(a["config_dir"])
        actions = []
        Log.info(f"\n{tag}Applying hub to account '{a['name']}' ({cdir})")
        if not dry:
            cdir.mkdir(parents=True, exist_ok=True)

        # files
        acct_units = collect_units(cdir, cfg["sync_items"])
        for rel, src in sorted(hub_units.items()):
            dst = cdir / rel
            if dst.exists() and hash_path(dst) == hash_path(src):
                continue
            if dst.exists() and not force and newest_mtime(dst) > newest_mtime(src) + 1:
                Log.warn(f"skip '{rel}': local copy is newer than the hub (run `sync`, or `apply --force`)")
                continue
            actions.append(f"file {'update' if dst.exists() else 'add'}: {rel}")
            if not dry:
                backup(dst, stamp, a["name"])
                copy_unit(src, dst)
        if prune:
            for rel, p in acct_units.items():
                if rel not in hub_units:
                    actions.append(f"file remove (prune): {rel}")
                    if not dry:
                        backup(p, stamp, a["name"])
                        remove_path(p)

        # settings.json
        sp = cdir / "settings.json"
        current = load_json(sp, {}) or {}
        desired = detokenize(restore_secrets(hub_settings, secrets, missing_vars), a)
        if isinstance(desired.get("env"), dict):          # env values are not expanded by Claude Code
            for k, v in list(desired["env"].items()):
                if isinstance(v, str) and PLACEHOLDER_RE.search(v):
                    desired["env"].pop(k)
        if prune:
            new_settings = merge_union(desired, local_only_part(current, cfg), "", [], "local")
        else:
            new_settings = merge_union(current, desired, "", [], "hub")
        if canon(new_settings) != canon(current):
            actions.append("settings.json merged")
            if not dry:
                backup(sp, stamp, a["name"])
                write_json_atomic(sp, new_settings)

        # MCP servers (user scope)
        gc_path = Path(a["global_config"])
        gc = load_json(gc_path, {}) or {}
        existing = gc.get("mcpServers") or {}
        for name, srv in sorted(manifest.get("mcpServers", {}).items()):
            want = detokenize(restore_secrets(srv, secrets, missing_vars), a)
            have = existing.get(name)
            if have is not None and (canon(have) == canon(want) or
                                     canon(tokenize(redact_mcp(name, have, {}), a)) == canon(srv)):
                continue                                   # same server; only the secret's location differs
            if have is not None and not force:
                Log.warn(f"MCP '{name}' differs in {a['name']}; left as is (use --force to overwrite)")
                continue
            actions.append(f"mcp {'replace' if have is not None else 'add'}: {name}")
            if dry:
                continue
            ok = False
            if cli.ok and not cli.is_shim and not getattr(args, "direct", False):
                if have is not None:
                    cli.run(a, ["mcp", "remove", name, "--scope", "user"])
                code, out, err = cli.run(a, ["mcp", "add-json", name, json.dumps(want), "--scope", "user"])
                ok = code == 0
                if not ok:
                    Log.warn(f"`claude mcp add-json {name}` failed ({err.strip()[:200]}); writing config directly")
            if not ok:
                backup(gc_path, stamp, a["name"])
                gc = load_json(gc_path, {}) or {}             # re-read: Claude rewrites this file often
                gc.setdefault("mcpServers", {})[name] = want
                write_json_atomic(gc_path, gc, mode=0o600)

        # marketplaces + plugins
        if manifest.get("plugins") or manifest.get("marketplaces"):
            if not cli.ok:
                Log.warn("`claude` CLI not found; plugins not installed (they are still declared in settings.json)")
            else:
                res = list_account_plugins(cli, a)
                cur_m, cur_p, _ = res if res else ({}, {}, [])
                for mname, spec in sorted(manifest.get("marketplaces", {}).items()):
                    if mname in cur_m:
                        continue
                    spec = detokenize(spec, a)
                    cmd = (["plugin", "marketplace", "add", "--claudeai", spec["name"]] if spec.get("type") == "claudeai"
                           else ["plugin", "marketplace", "add", spec["source"]])
                    actions.append(f"marketplace add: {mname}")
                    if not dry:
                        code, out, err = cli.run(a, cmd, timeout=600)
                        if code != 0:
                            Log.warn(f"marketplace '{mname}' failed: {(err or out).strip()[:200]}")
                for pid, meta in sorted(manifest.get("plugins", {}).items()):
                    want_on = bool(meta.get("enabled", True))
                    if pid not in cur_p:
                        actions.append(f"plugin install: {pid}")
                        if not dry:
                            code, out, err = cli.run(a, ["plugin", "install", pid, "--scope", "user"], timeout=600)
                            if code != 0:
                                Log.warn(f"plugin '{pid}' failed: {(err or out).strip()[:200]} — "
                                         f"if it asks to run a command, install it once by hand: "
                                         f"claude plugin install {pid} -y")
                                continue
                            if not want_on:
                                cli.run(a, ["plugin", "disable", pid, "--scope", "user"])
                    elif want_on and not cur_p[pid]:
                        actions.append(f"plugin enable: {pid}")
                        if not dry:
                            cli.run(a, ["plugin", "enable", pid, "--scope", "user"])

        for line in actions:
            Log.info(f"  {tag}{line}")
        if not actions:
            Log.info("  already up to date")
        summary.append((a["name"], len(actions)))

    if missing_vars:
        Log.warn("These secrets were redacted on another machine and aren't in this machine's "
                 "~/.account-sync/secrets.json. MCP servers referencing them read the value from an "
                 "environment variable of the same name; add them to secrets.json or export them: "
                 + ", ".join(sorted(missing_vars)))
    report_account_level(manifest)
    if not dry and (BACKUP_DIR / stamp).exists():
        Log.info(f"\nBackups of everything changed: {BACKUP_DIR / stamp}")
    return summary


def report_account_level(manifest):
    """Connectors and claude.ai-synced skills/plugins live in each claude.ai account."""
    accts = manifest.get("accounts", {})
    for field, label, where in (
        ("connectors", "claude.ai connectors", "https://claude.ai/customize/connectors"),
        ("claudeai_skills", "claude.ai skills", "that account's skills settings on claude.ai"),
        ("claudeai_plugins", "claude.ai plugins", "that account's plugin settings on claude.ai"),
    ):
        known = {k: set(v.get(field, [])) for k, v in accts.items() if field in v}
        if len(known) < 2:
            continue
        union = set().union(*known.values())
        gaps = {k: sorted(union - s) for k, s in known.items() if union - s}
        if gaps:
            Log.info(f"\n{label}: these live in each claude.ai account and can't be written locally. "
                     f"Sign in to the account and add them at {where}:")
            for k, miss in sorted(gaps.items()):
                Log.info(f"  {k}: missing {', '.join(miss)}")


# ─────────────────────────────── lock / state ───────────────────────────────

class Lock:
    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime > 3600:
            LOCK_PATH.unlink(missing_ok=True)
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
        except FileExistsError:
            self.held = False
        return self

    def __exit__(self, *exc):
        if self.held:
            LOCK_PATH.unlink(missing_ok=True)


# ─────────────────────────────── commands ───────────────────────────────

def cmd_discover(args):
    accts = discover_accounts()
    print(f"Found {len(accts)} Claude Code config dir(s) on {HOSTNAME}:")
    for a in accts:
        print(f"  {a['name']:<14} {a['config_dir']}   (global config: {a['global_config']})")


def cmd_init(args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)       # backups and secrets.json live here
    except OSError:
        pass
    cfg = load_config(required=False) or default_config()
    if args.hub:
        cfg["hub"] = str(Path(args.hub).expanduser().resolve())
    found = discover_accounts()
    known = {a["config_dir"] for a in cfg["accounts"]}
    for a in found:
        if a["config_dir"] not in known:
            cfg["accounts"].append(a)
    for extra in args.add or []:
        rec = account_record(Path(extra))
        if rec["config_dir"] not in {a["config_dir"] for a in cfg["accounts"]}:
            cfg["accounts"].append(rec)
    if args.primary:
        cfg["primary"] = args.primary
    cfg["claude_bin"] = cfg.get("claude_bin") or shutil.which("claude")
    hub = Path(cfg["hub"])
    if args.remote and not hub.exists():
        code = subprocess.run(["git", "clone", "-q", args.remote, str(hub)], capture_output=True).returncode
        if code != 0:
            hub.mkdir(parents=True)
            git(hub, "init", "-q", check=True)
            git(hub, "remote", "add", "origin", args.remote)
    elif args.remote:
        if not hub_is_git(hub):
            git(hub, "init", "-q", check=True)
        if not hub_has_remote(hub):
            git(hub, "remote", "add", "origin", args.remote)
    elif not hub.exists():
        hub.mkdir(parents=True)
        git(hub, "init", "-q")
    if hub.exists():
        ensure_gitattributes(hub)
    save_config(cfg)
    me = Path(__file__).resolve()
    stable = STATE_DIR / "account_sync.py"
    if me != stable:
        shutil.copy2(me, stable)
    note = install_command(TOOL, stable)
    for legacy in (HOME / ".local" / "bin" / "claude-sync", HOME / ".local" / "bin" / "claude-sync.cmd"):
        try:                                   # the command from before the rename
            if legacy.exists() and LEGACY_HOOK_MARKER in legacy.read_text(encoding="utf-8", errors="ignore"):
                legacy.unlink()
        except OSError:
            pass
    print(f"Config: {CONFIG_PATH}\nHub:    {cfg['hub']}{'  (remote: ' + args.remote + ')' if args.remote else ''}")
    print(f"claude CLI: {cfg['claude_bin'] or 'NOT FOUND — install Claude Code, or set claude_bin in ' + str(CONFIG_PATH)}")
    if not shutil.which("git"):
        print("git: not found — fine on one machine; to share between machines install git "
              + {"macos": "(xcode-select --install)", "windows": "(winget install --id Git.Git -e)",
                 "linux": "(e.g. sudo apt install git)"}[PLATFORM])
    print("Accounts:")
    for a in cfg["accounts"]:
        print(f"  {a['name']:<14} {a['config_dir']}")
    print(f"\n{note}\nNext:  {TOOL} status        (preview, changes nothing)\n       {TOOL} sync\n       {TOOL} schedule")
    print(f"(Until the `{TOOL}` command works, use:  {py_cmd()} {shown(stable)} …)")


def cmd_accounts(args):
    cfg = load_config()
    if args.action == "add":
        rec = account_record(Path(args.path), args.name)
        cfg["accounts"] = [a for a in cfg["accounts"] if a["config_dir"] != rec["config_dir"]] + [rec]
    elif args.action == "remove":
        cfg["accounts"] = [a for a in cfg["accounts"] if a["name"] != args.name]
    save_config(cfg)
    for a in cfg["accounts"]:
        print(f"  {a['name']:<14} {a['config_dir']}")


def _should_skip_for_interval(cfg, args):
    if not getattr(args, "auto", False):
        return False
    st = load_json(STATE_PATH, {}) or {}
    last = st.get("last_success", 0)
    return time.time() - last < cfg.get("min_interval_minutes", 30) * 60


def cmd_sync(args):
    if getattr(args, "background", False):
        cmd = [pythonw(), str(Path(__file__).resolve())] + [x for x in sys.argv[1:] if x != "--background"]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as log:
            kw = {"start_new_session": True} if not IS_WINDOWS else {"creationflags": 0x00000008 | 0x00000200}
            subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, **kw)
        return
    cfg = load_config()
    if _should_skip_for_interval(cfg, args):
        return
    with Lock() as lk:
        if not lk.held:
            Log.info("Another account-sync run is in progress; skipping.")
            return
        Log.info(f"── account-sync {VERSION} · {dt.datetime.now():%Y-%m-%d %H:%M:%S} · {HOSTNAME}")
        if args.dry_run:
            Log.info("[dry-run] harvesting into a scratch copy of the hub; nothing on disk will change.")
            preview(cfg, args)
            return
        manifest = harvest(cfg, args)
        apply(cfg, args, manifest)
        write_report(cfg)
        git_commit_push(Path(cfg["hub"]), args.no_git, f"sync from {HOSTNAME} {now_stamp()}")
        st = load_json(STATE_PATH, {}) or {}
        st["last_success"] = time.time()
        write_json_atomic(STATE_PATH, st)
        if sys.stdout is None:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("\n".join(Log.lines) + "\n")


def preview(cfg, args):
    """Harvest into a throwaway copy of the hub, then dry-run apply from it. Changes nothing."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="account-sync-preview-"))
    real_hub = Path(cfg["hub"])
    if real_hub.exists():
        shutil.copytree(real_hub, tmp / "hub", ignore=shutil.ignore_patterns(".git"))
    preview_cfg = dict(cfg, hub=str(tmp / "hub"))
    saved = SECRETS_PATH.read_bytes() if SECRETS_PATH.exists() else None
    a2 = argparse.Namespace(**vars(args))
    a2.no_git, a2.dry_run = True, True
    try:
        manifest = harvest(preview_cfg, a2)
        apply(preview_cfg, a2, manifest)
    finally:
        if saved is None:
            SECRETS_PATH.unlink(missing_ok=True)
        else:
            SECRETS_PATH.write_bytes(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_harvest(args):
    cfg = load_config()
    with Lock() as lk:
        if not lk.held:
            print("Another account-sync run is in progress.")
            return
        harvest(cfg, args)
        write_report(cfg)
        git_commit_push(Path(cfg["hub"]), args.no_git, f"harvest from {HOSTNAME} {now_stamp()}")


def cmd_apply(args):
    cfg = load_config()
    with Lock() as lk:
        if not lk.held:
            print("Another account-sync run is in progress.")
            return
        apply(cfg, args)


def write_report(cfg):
    hub = Path(cfg["hub"])
    body = "\n".join(Log.lines)
    (hub / "LAST_RUN.md").write_text(f"# Last account-sync run ({HOSTNAME})\n\n```\n{body}\n```\n", encoding="utf-8")


def hook_command():
    script = STATE_DIR / "account_sync.py"
    if IS_WINDOWS:
        # Forward slashes and a bare command word work the same in Git Bash, PowerShell and cmd.
        exe = "py -3" if shutil.which("py") else Path(sys.executable).as_posix()
        return f'{exe} "{script.as_posix()}" sync --auto --quiet --background'
    try:
        rel = script.relative_to(HOME).as_posix()
        target = f'"$HOME/{rel}"'
    except ValueError:
        target = f'"{script.as_posix()}"'
    return f"python3 {target} sync --auto --quiet --background"


def cmd_install_hook(args):
    cfg = load_config()
    entry = {"matcher": "startup", "hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]}
    stamp = now_stamp()
    for a in cfg["accounts"]:
        sp = Path(a["config_dir"]) / "settings.json"
        s = load_json(sp, {}) or {}
        groups = s.setdefault("hooks", {}).setdefault("SessionStart", [])
        groups[:] = [g for g in groups if not any(is_our_hook(h) for h in g.get("hooks", []))]
        if not args.remove:
            groups.append(entry)
        if not groups:
            s["hooks"].pop("SessionStart")
        if not s["hooks"]:
            s.pop("hooks")
        backup(sp, stamp, a["name"])
        write_json_atomic(sp, s)
        print(f"{'Removed' if args.remove else 'Installed'} SessionStart hook in {sp}")


LAUNCHD_LABEL = "com.account-sync"
LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _parse_hhmm(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SystemExit(f"--daily expects a 24-hour time like 09:00, got {s!r}")
    return int(m.group(1)), int(m.group(2))


def _launchd_plist(cmd, hour=None, minute=None, every_sec=None, path_env=""):
    import plistlib
    d = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": cmd,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "EnvironmentVariables": {"PATH": path_env, "HOME": str(HOME)},
        "RunAtLoad": False,
    }
    if every_sec:
        d["StartInterval"] = every_sec
    else:
        d["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}   # missed runs fire on wake
    return plistlib.dumps(d).decode()


def remove_legacy_jobs():
    """Remove the daily job that versions before the rename (claude-sync) installed."""
    try:
        if IS_WINDOWS:
            subprocess.run(["schtasks", "/Delete", "/TN", "ClaudeSync", "/F"], capture_output=True)
        elif IS_MAC:
            old = HOME / "Library" / "LaunchAgents" / "com.claude-sync.plist"
            if old.exists():
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(old)], capture_output=True)
                old.unlink(missing_ok=True)
        elif shutil.which("crontab"):
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            if cur.returncode == 0 and "# claude-sync" in cur.stdout:
                keep = [l for l in cur.stdout.splitlines() if "# claude-sync" not in l]
                subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True)
    except OSError:
        pass


def migrate_legacy_state():
    """Carry over a setup made before the tool was renamed from claude-sync (~/.claude-sync)."""
    if os.environ.get("ACCOUNT_SYNC_HOME") or STATE_DIR.exists() or not (LEGACY_STATE / "config.json").exists():
        return
    shutil.copytree(LEGACY_STATE, STATE_DIR, symlinks=True)
    cfg = load_json(CONFIG_PATH, {}) or {}
    if str(cfg.get("hub", "")).startswith(str(LEGACY_STATE)):
        cfg["hub"] = str(STATE_DIR) + cfg["hub"][len(str(LEGACY_STATE)):]
        write_json_atomic(CONFIG_PATH, cfg)
    old_script = STATE_DIR / "claude_sync.py"
    if old_script.exists():
        old_script.unlink()
    print(f"Moved your setup from {LEGACY_STATE} to {STATE_DIR} (claude-sync is now account-sync). "
          f"The old folder is untouched; delete it when you're happy. Re-run `schedule` and `install-hook` "
          f"if you used them.", file=sys.stderr)


def cmd_schedule(args):
    """Install a recurring `sync`: launchd on macOS, cron on Linux/WSL, Task Scheduler on Windows."""
    cfg = load_config()
    if not cfg.get("claude_bin"):
        cfg["claude_bin"] = shutil.which("claude")
        save_config(cfg)
    script = STATE_DIR / "account_sync.py"
    daily = None if args.every else _parse_hhmm(args.daily or "09:00")
    every = max(15, int(args.every)) if args.every else None
    when = f"daily at {daily[0]:02d}:{daily[1]:02d}" if daily else f"every {every} minutes"
    cmd = [sys.executable, str(script), "sync", "--quiet"]
    # schedulers start with a bare PATH; make sure claude and git are findable
    path_dirs = [os.path.dirname(p) for p in (cfg.get("claude_bin"), shutil.which("git"), sys.executable) if p]
    path_env = os.pathsep.join(dict.fromkeys(path_dirs + ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]))

    if not args.print:
        remove_legacy_jobs()
    if IS_WINDOWS:
        if args.remove:
            subprocess.run(["schtasks", "/Delete", "/TN", "AccountSync", "/F"])
            print("Removed scheduled task 'AccountSync'.")
            return
        tr = subprocess.list2cmdline([pythonw()] + cmd[1:])   # pythonw: no console window flashing up
        if daily:
            spec = ["/SC", "DAILY", "/ST", f"{daily[0]:02d}:{daily[1]:02d}"]
        elif every < 1440:
            spec = ["/SC", "MINUTE", "/MO", str(every)]
        else:
            spec = ["/SC", "DAILY", "/MO", str(every // 1440)]
        full = ["schtasks", "/Create", "/TN", "AccountSync", "/TR", tr, "/F"] + spec
        if args.print:
            print(subprocess.list2cmdline(full))
            return
        if len(tr) > 261:
            raise SystemExit("The task command is longer than Task Scheduler allows (261 characters). "
                             "Move Python or your user folder to a shorter path, or create the task by hand:\n  " + tr)
        r = subprocess.run(full, capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode == 0:
            print(f"Scheduled task 'AccountSync' runs {when}.")
        return

    if sys.platform == "darwin":
        uid = str(os.getuid())
        if args.remove:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(LAUNCHD_PLIST)], capture_output=True)
            LAUNCHD_PLIST.unlink(missing_ok=True)
            print("Removed the account-sync launchd job.")
            return
        plist = _launchd_plist(cmd, *(daily or (None, None)), every_sec=every * 60 if every else None, path_env=path_env)
        if args.print:
            print(plist)
            return
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.write_text(plist, encoding="utf-8")
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCHD_PLIST)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip()}")
        print(f"launchd job '{LAUNCHD_LABEL}' runs {when} (a run missed while the Mac was asleep happens on wake).")
        return

    quoted = " ".join(f'"{c}"' for c in cmd)
    if daily:
        timing = f"{daily[1]} {daily[0]} * * *"
    elif every < 60:
        timing = f"*/{every} * * * *"
    elif every < 1440:
        timing = f"7 */{every // 60} * * *"
    else:
        timing = "7 9 * * *"
    line = f'{timing} PATH="{path_env}" {quoted} >> "{LOG_PATH}" 2>&1 # account-sync'
    if args.print:
        print(line)
        return
    if not shutil.which("crontab"):
        raise SystemExit("cron isn't installed. Install it (e.g. sudo apt install cron), "
                         "or add this line to your scheduler:\n  " + line)
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [l for l in (cur.stdout.splitlines() if cur.returncode == 0 else [])
             if "# account-sync" not in l and "# claude-sync" not in l]
    if not args.remove:
        lines.append(line)
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    print("Removed the account-sync cron entry." if args.remove else f"cron entry installed ({when}):\n  {line}")


def cmd_status(args):
    args.dry_run = True
    args.no_git = True
    cmd_sync(args)


# ─────────────────────────────── settings page ───────────────────────────────
# `account-sync ui` serves one page on 127.0.0.1 with a random token in its link. Other OS
# accounts on the same computer can reach localhost too, so every request must carry the token.

CATEGORY_TITLES = {"skills": "Skills", "agents": "Agents", "commands": "Commands", "rules": "Rules",
                   "output-styles": "Output styles", "workflows": "Workflows", "themes": "Themes",
                   "hooks": "Hooks", "scripts": "Scripts"}
UI_ITEM_RE = re.compile(r"^[A-Za-z0-9._*?\[\]-]{1,80}$")
UI_KEY_RE = re.compile(r"^[A-Za-z0-9_.@:/-]{1,120}$")


def _ui_cell(present, ref, h):
    if not present:
        return "missing"
    return "current" if h == ref else "older"


def ui_inventory(cfg):
    """Who has what: one row per synced item, one cell per account on this computer."""
    accts = cfg["accounts"]
    hub = Path(cfg["hub"])
    manifest = load_json(hub / "manifest.json", {}) or {}
    groups = {}

    # files (skills, agents, commands, CLAUDE.md, ...)
    versions = {}
    for src, root in [("hub", hub / "files")] + [(a["name"], Path(a["config_dir"])) for a in accts]:
        for rel, p in collect_units(root, cfg["sync_items"]).items():
            versions.setdefault(rel, {})[src] = (hash_path(p), newest_mtime(p))
    for rel, vs in sorted(versions.items()):
        ref = max(vs.values(), key=lambda v: v[1])[0]
        head, _, rest = rel.partition("/")
        title = CATEGORY_TITLES.get(head, "Files") if rest else "Files"
        groups.setdefault(title, []).append({
            "name": rest or rel, "path": rel,
            "cells": {a["name"]: _ui_cell(a["name"] in vs, ref, vs.get(a["name"], (None,))[0]) for a in accts},
            "hub": _ui_cell("hub" in vs, ref, vs.get("hub", (None,))[0]),
        })

    # MCP servers: compare with secrets stripped and paths tokenized, as the sync does
    mcp = {}
    for a in accts:
        servers = (load_json(Path(a["global_config"]), {}) or {}).get("mcpServers") or {}
        for name, srv in servers.items():
            mcp.setdefault(name, {})[a["name"]] = canon(tokenize(redact_mcp(name, srv, {}), a))
    for name, srv in (manifest.get("mcpServers") or {}).items():
        mcp.setdefault(name, {})["hub"] = canon(srv)
    rows = []
    for name, vs in sorted(mcp.items()):
        ref = vs.get("hub") or next(iter(vs.values()))
        rows.append({"name": name, "path": f"MCP server {name}",
                     "cells": {a["name"]: _ui_cell(a["name"] in vs, ref, vs.get(a["name"])) for a in accts},
                     "hub": _ui_cell("hub" in vs, ref, vs.get("hub"))})
    if rows:
        groups["MCP servers"] = rows

    # plugins: the CLI knows what is installed; settings.json knows what is enabled
    cli = ClaudeCLI(cfg)
    have = {}
    for a in accts:
        ids = set(((load_json(Path(a["config_dir"]) / "settings.json", {}) or {}).get("enabledPlugins") or {}))
        if cli.ok:
            res = list_account_plugins(cli, a)
            if res:
                ids |= set(res[1])
        have[a["name"]] = ids
    hub_plugins = set(manifest.get("plugins") or {})
    all_plugins = set().union(hub_plugins, *have.values()) if have else hub_plugins
    if all_plugins:
        groups["Plugins"] = [{"name": pid, "path": f"plugin {pid}",
                              "cells": {n: "current" if pid in ids else "missing" for n, ids in have.items()},
                              "hub": "current" if pid in hub_plugins else "missing"} for pid in sorted(all_plugins)]

    # claude.ai connectors: known only after a sync; they must be added by hand on claude.ai
    recs = manifest.get("accounts") or {}
    conns = {a["name"]: set(recs.get(f"{HOSTNAME}/{a['name']}", {}).get("connectors", []))
             for a in accts if "connectors" in recs.get(f"{HOSTNAME}/{a['name']}", {})}
    every = set().union(*conns.values()) if conns else set()
    if every:
        groups["claude.ai connectors"] = [{
            "name": c.replace("claude.ai ", ""), "path": c, "manual": True,
            "cells": {a["name"]: ("current" if c in conns.get(a["name"], set()) else
                                  ("manual" if a["name"] in conns else "unknown")) for a in accts},
            "hub": "none"} for c in sorted(every)]

    order = ["Skills", "Agents", "Commands", "Rules", "Output styles", "Workflows", "Themes", "Hooks",
             "Scripts", "Files", "MCP servers", "Plugins", "claude.ai connectors"]
    out = [{"title": t, "rows": groups[t]} for t in order if t in groups]
    missing = {a["name"]: sum(1 for g in out for r in g["rows"] if r["cells"].get(a["name"]) in ("missing", "older", "manual"))
               for a in accts}
    return out, missing


def _leaves(d, path=()):
    out = {}
    for k, v in d.items():
        p = path + (k,)
        if isinstance(v, dict) and v:
            out.update(_leaves(v, p))
        elif not isinstance(v, list):          # lists are merged, so they never conflict
            out[p] = v
    return out


def _ui_show(keys, v):
    if (SECRET_KEY_RE.search(keys[-1]) or (isinstance(v, str) and SECRET_VAL_RE.search(v))) and not (
            isinstance(v, str) and _is_placeholder(v)):
        return "(hidden)"
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("{{CLAUDE_CONFIG_DIR}}", "<account folder>").replace("{{HOME}}", "~")
    return s if len(s) <= 120 else s[:117] + "..."


def _shared_settings(cfg, a):
    sp = Path(a["config_dir"]) / "settings.json"
    if not sp.exists():
        return None, 0
    s = strip_local_only(load_json(sp, {}) or {}, cfg)
    shared, _ = split_os_keys(tokenize(s, a))
    return shared, sp.stat().st_mtime


def ui_conflicts(cfg):
    """settings.json values that differ between accounts. The newest edit wins at the next sync."""
    per, mtimes = {}, {}
    for a in cfg["accounts"]:
        shared, mtime = _shared_settings(cfg, a)
        if shared is None:
            continue
        mtimes[a["name"]] = mtime
        for keys, v in _leaves(shared).items():
            per.setdefault(keys, {})[a["name"]] = v
    out = []
    for keys, vals in sorted(per.items()):
        if len({canon(v) for v in vals.values()}) < 2:
            continue
        winner = max(vals, key=lambda n: (n == cfg.get("primary"), mtimes.get(n, 0)))
        out.append({"keys": list(keys), "label": ".".join(keys),
                    "values": [{"account": n, "value": _ui_show(keys, v), "wins": n == winner}
                               for n, v in sorted(vals.items())]})
    return out


def ui_resolve(cfg, keys, source):
    """Give every account (and the hub) the value one account has for this setting."""
    src = next((a for a in cfg["accounts"] if a["name"] == source), None)
    if src is None or not keys or not all(isinstance(k, str) for k in keys):
        raise ValueError("unknown account or setting")
    if keys[0] in OS_SPECIFIC_KEYS or keys[0] in cfg["settings_exclude"]:
        raise ValueError("that setting is kept separately per computer or account")
    shared, _ = _shared_settings(cfg, src)
    node = shared
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise ValueError("that account doesn't have this setting")
        node = node[k]
    value = node                                    # tokenized: paths are placeholders here

    def put(d, val):
        for k in keys[:-1]:
            if not isinstance(d.get(k), dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = copy.deepcopy(val)

    stamp, changed = now_stamp(), []
    for a in cfg["accounts"]:
        sp = Path(a["config_dir"]) / "settings.json"
        if not sp.exists():
            continue
        s = load_json(sp, {}) or {}
        before = canon(s)
        put(s, detokenize(value, a))
        if canon(s) != before:
            backup(sp, stamp, a["name"])
            write_json_atomic(sp, s)
            changed.append(a["name"])
    # The hub is left alone on purpose: the next sync reads the accounts (now all equal and newest)
    # and strips secrets on the way in, exactly as for any other edit.
    return changed


def _secret_uses(hub):
    """Placeholder name -> where it is used, in words ("github MCP server, header Authorization")."""
    uses = {}

    def walk(x, where, path):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, where, path + [k])
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, where, path + [f"#{i + 1}"])
        elif isinstance(x, str):
            for var in PLACEHOLDER_RE.findall(x):
                p = path[:]
                if len(p) >= 2 and p[0] in ("env", "headers"):
                    what = ("variable " if p[0] == "env" else "header ") + p[1]
                elif p and p[0] == "args":
                    what = "argument " + p[-1].lstrip("#")
                elif p and p[0] == "url":
                    what = "URL"
                else:
                    what = ".".join(p)
                uses.setdefault(var, f"{where}, {what}")

    manifest = load_json(hub / "manifest.json", {}) or {}
    for name, srv in (manifest.get("mcpServers") or {}).items():
        walk(srv, f"{name} MCP server", [])
    for f in [hub / "settings.json"] + sorted(hub.glob("settings.*.json")):
        walk(load_json(f, {}) or {}, "settings.json", [])
    return uses


def ui_secrets(cfg):
    uses = _secret_uses(Path(cfg["hub"]))
    have = load_json(SECRETS_PATH, {}) or {}
    return [{"name": n, "label": uses.get(n, n[len("CSYNC_"):].replace("_", " ").lower()),
             "set": n in have, "used": n in uses}
            for n in sorted(set(uses) | set(have))]


def ui_set_secret(name, value):
    if not re.fullmatch(r"CSYNC_[A-Z0-9_]{1,120}", name or "") or not isinstance(value, str) \
            or not value.strip() or len(value) > 8192:
        raise ValueError("enter a value")
    secrets = load_json(SECRETS_PATH, {}) or {}
    secrets[name] = value.strip()
    write_json_atomic(SECRETS_PATH, secrets, mode=0o600)


def ui_schedule():
    """What the scheduler on this computer is set to run, if anything."""
    try:
        if IS_WINDOWS:
            r = subprocess.run(["schtasks", "/Query", "/TN", "AccountSync"], capture_output=True, text=True)
            return {"on": r.returncode == 0, "how": "Task Scheduler", "time": None}
        if IS_MAC:
            if not LAUNCHD_PLIST.exists():
                return {"on": False, "how": "launchd", "time": None}
            import plistlib
            pl = plistlib.loads(LAUNCHD_PLIST.read_bytes())
            sci = pl.get("StartCalendarInterval") or {}
            t = f"{sci['Hour']:02d}:{sci['Minute']:02d}" if "Hour" in sci else None
            return {"on": True, "how": "launchd", "time": t,
                    "every": (pl.get("StartInterval") or 0) // 60 or None}
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True) if shutil.which("crontab") else None
        for line in (r.stdout.splitlines() if r and r.returncode == 0 else []):
            if "# account-sync" in line:
                m = re.match(r"^(\d+) (\d+) \* \* \*", line)
                return {"on": True, "how": "cron", "time": f"{int(m.group(2)):02d}:{int(m.group(1)):02d}" if m else None}
        return {"on": False, "how": "cron", "time": None}
    except Exception:
        return {"on": False, "how": "", "time": None}


def ui_machines(cfg):
    hub = Path(cfg["hub"])
    manifest = load_json(hub / "manifest.json", {}) or {}
    machines = {}
    for key in manifest.get("accounts") or {}:
        host, _, name = key.partition("/")
        machines.setdefault(host, {"name": host, "accounts": [], "last": None})["accounts"].append(name)
    machines.setdefault(HOSTNAME, {"name": HOSTNAME, "accounts": [a["name"] for a in cfg["accounts"]], "last": None})
    if hub_is_git(hub):
        code, out, _ = git(hub, "log", "-n", "400", "--format=%cI%x09%s")
        for line in (out if code == 0 else "").splitlines():
            when, _, subject = line.partition("\t")
            m = re.match(r"^(?:sync|harvest) from (\S+)", subject)
            if m and m.group(1) in machines and not machines[m.group(1)]["last"]:
                machines[m.group(1)]["last"] = when
    last = (load_json(STATE_PATH, {}) or {}).get("last_success")
    if last:
        machines[HOSTNAME]["last"] = dt.datetime.fromtimestamp(last).astimezone().isoformat(timespec="seconds")
    out = sorted(machines.values(), key=lambda m: (m["name"] != HOSTNAME, m["name"]))
    for m in out:
        m["this"] = m["name"] == HOSTNAME
        m["accounts"] = sorted(set(m["accounts"]))
    return out


def ui_state():
    cfg = load_config()
    groups, missing = ui_inventory(cfg)
    hub = Path(cfg["hub"])
    code, out, _ = git(hub, "remote", "get-url", "origin") if hub_is_git(hub) else (1, "", "")
    remote = re.sub(r"(?<=://)[^/@\s]+@", "", out.strip()) if code == 0 and out.strip() else None
    hooked = any(is_our_hook(h) for a in cfg["accounts"]
                 for g in ((load_json(Path(a["config_dir"]) / "settings.json", {}) or {}).get("hooks") or {}).get("SessionStart", [])
                 for h in g.get("hooks", []))
    return {
        "version": VERSION, "machine": HOSTNAME, "platform": PLATFORM,
        "accounts": [{"name": a["name"], "folder": shown(a["config_dir"]), "primary": a["name"] == cfg.get("primary")}
                     for a in cfg["accounts"]],
        "groups": groups, "missing": missing,
        "conflicts": ui_conflicts(cfg),
        "secrets": ui_secrets(cfg),
        "machines": ui_machines(cfg),
        "config": {
            "sync_items": cfg["sync_items"], "default_sync_items": DEFAULT_SYNC_ITEMS,
            "settings_exclude": cfg["settings_exclude"], "env_exclude": cfg["env_exclude"],
            "primary": cfg.get("primary"), "inventory_connectors": bool(cfg.get("inventory_connectors", True)),
            "hub": shown(cfg["hub"]),
            "remote": remote,
        },
        "schedule": ui_schedule(), "hook": hooked,
    }


def ui_save_config(body):
    cfg = load_config()
    lists = {"sync_items": UI_ITEM_RE, "settings_exclude": UI_KEY_RE, "env_exclude": UI_KEY_RE}
    for field, rx in lists.items():
        if field in body:
            vals = body[field]
            if not isinstance(vals, list) or not all(isinstance(v, str) and rx.fullmatch(v) and v not in ("..", ".")
                                                     for v in vals):
                raise ValueError(f"{field}: use plain names only")
            if field == "sync_items" and any(v.casefold() in NEVER_TOUCH_CF for v in vals):
                raise ValueError("that folder holds account data and can't be synced")
            cfg[field] = list(dict.fromkeys(vals))
    if "primary" in body:
        if body["primary"] not in [None] + [a["name"] for a in cfg["accounts"]]:
            raise ValueError("unknown account")
        cfg["primary"] = body["primary"]
    if "inventory_connectors" in body:
        cfg["inventory_connectors"] = bool(body["inventory_connectors"])
    save_config(cfg)


def ui_job_argv(action, body):
    """Map a button to a command line of this script. Nothing from the page reaches a shell."""
    if action == "preview":
        return ["status"]
    if action == "sync":
        return ["sync"]
    if action == "schedule":
        if body.get("off"):
            return ["schedule", "--remove"]
        t = body.get("time") or ""
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ValueError("enter a time like 09:00")
        return ["schedule", "--daily", t]
    if action == "hook":
        return ["install-hook"] + (["--remove"] if body.get("off") else [])
    raise ValueError("unknown action")


ACCOUNT_SYNC_UI = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Account Sync settings</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='9' cy='9' r='6' fill='%230B7A64'/%3E%3Ccircle cx='23' cy='9' r='6' fill='%230B7A64'/%3E%3Ccircle cx='9' cy='23' r='6' fill='%230B7A64'/%3E%3Ccircle cx='23' cy='23' r='5' fill='none' stroke='%23B86E00' stroke-width='2.5'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#EDF0F3; --surface:#FFFFFF; --sunk:#E3E8ED; --ink:#17212B; --muted:#5C6876; --rule:#D2D9E0;
  --ok:#0B7A64; --ok-soft:#D7EEE8; --drift:#B86E00; --drift-soft:#F7E9D2; --danger:#B42318; --danger-soft:#F8DEDA;
  --focus:#0B7A64; --btn:#17212B; --btn-ink:#FFFFFF;
  --sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, "Cascadia Mono", "Segoe UI Mono", Menlo, Consolas, "Liberation Mono", monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0F1419; --surface:#161D24; --sunk:#1D262F; --ink:#E4E9EE; --muted:#97A3B0; --rule:#2A3541;
    --ok:#3CC2A2; --ok-soft:#173A33; --drift:#E8A33D; --drift-soft:#3A2C14; --danger:#F2786D; --danger-soft:#3D1D1A;
    --focus:#3CC2A2; --btn:#E4E9EE; --btn-ink:#0F1419; color-scheme: dark; }
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg); color:var(--ink); font:15px/1.5 var(--sans); -webkit-font-smoothing:antialiased}
a{color:inherit; text-underline-offset:3px}
button,input,select,textarea{font:inherit; color:inherit}
:focus-visible{outline:2px solid var(--focus); outline-offset:2px}
code,.mono{font-family:var(--mono); font-size:.9em}

.top{display:flex; align-items:center; gap:16px; flex-wrap:wrap; padding:18px 28px; border-bottom:1px solid var(--rule); background:var(--surface)}
.brand{display:flex; align-items:center; gap:10px; font-weight:650; font-size:17px}
.brand svg{flex:none}
.where{color:var(--muted); font-size:14px}
.actions{margin-left:auto; display:flex; gap:8px}
.btn{border:1px solid var(--rule); background:var(--surface); border-radius:7px; padding:8px 14px; cursor:pointer; font-weight:550; white-space:nowrap}
.btn:hover{background:var(--sunk)}
.btn.primary{background:var(--btn); color:var(--btn-ink); border-color:var(--btn)}
.btn.primary:hover{opacity:.9}
.btn.danger{color:var(--danger); border-color:color-mix(in srgb, var(--danger) 45%, var(--rule))}
.btn.small{padding:4px 10px; font-size:13px}
.btn:disabled{opacity:.5; cursor:default}

.shell{display:grid; grid-template-columns:240px minmax(0,1fr); max-width:1280px; margin:0 auto}
nav.tabs{padding:24px 12px 24px 28px; position:sticky; top:0; align-self:start}
nav.tabs a{display:flex; justify-content:space-between; align-items:center; gap:8px; white-space:nowrap; padding:8px 12px; border-radius:7px; text-decoration:none; color:var(--muted); font-weight:550}
nav.tabs a:hover{color:var(--ink); background:var(--sunk)}
nav.tabs a[aria-current="page"]{color:var(--ink); background:var(--surface); box-shadow:inset 3px 0 0 var(--ok)}
.count{font-variant-numeric:tabular-nums; font-size:12px; min-width:22px; text-align:center; padding:1px 7px; border-radius:20px; background:var(--drift-soft); color:var(--drift); font-weight:650}
main{padding:28px 28px 120px; min-width:0}
section[hidden]{display:none}
h1{font-size:26px; line-height:1.25; font-weight:650; margin:0 0 6px; max-width:30em; letter-spacing:-.01em}
h2{font-size:19px; margin:0 0 6px; font-weight:650}
h3{font-size:15px; margin:28px 0 8px; font-weight:650}
.lede{color:var(--muted); margin:0 0 22px; max-width:46em}
.muted{color:var(--muted)}

/* the pegboard */
.legend{display:flex; flex-wrap:wrap; gap:18px; font-size:13px; color:var(--muted); margin:0 0 12px}
.legend span{display:inline-flex; align-items:center; gap:7px}
.board{overflow-x:auto; background:var(--surface); border:1px solid var(--rule); border-radius:10px}
table.peg{border-collapse:collapse; width:100%; min-width:520px}
table.peg th, table.peg td{padding:7px 14px; text-align:left}
table.peg thead th{position:sticky; top:0; background:var(--surface); border-bottom:1px solid var(--rule); font-weight:600; vertical-align:bottom; z-index:1}
table.peg thead th small{display:block; font-weight:400; color:var(--muted); font-size:12px; font-family:var(--mono)}
table.peg thead th.acct, table.peg td{text-align:center; width:1%; white-space:nowrap}
table.peg thead th.hubcol, table.peg td.hubcol{border-left:1px dashed var(--rule)}
table.peg tr.group th{padding-top:18px; font-size:13px; color:var(--muted); font-weight:600; background:var(--surface)}
table.peg tbody tr.item:hover{background:var(--sunk)}
table.peg tbody tr.item th{font-family:var(--mono); font-weight:400; font-size:13.5px; overflow-wrap:anywhere; min-width:150px}
table.peg thead th:first-child, table.peg tbody tr.item th{position:sticky; left:0; background:var(--surface); z-index:2}
table.peg tbody tr.item:hover th{background:var(--sunk)}
.badge{display:inline-block; font-size:11.5px; padding:1px 7px; border-radius:20px; background:var(--sunk); color:var(--muted); margin-left:6px; font-family:var(--sans); font-weight:550}
.peg-dot{display:inline-block; width:14px; height:14px; border-radius:50%; vertical-align:middle}
.peg-dot.current{background:var(--ok)}
.peg-dot.older{border:2px solid var(--ok); background:linear-gradient(90deg, var(--ok) 50%, transparent 50%)}
.peg-dot.missing{border:2px solid var(--drift)}
.peg-dot.manual{border:2px dotted var(--drift)}
.peg-dot.unknown,.peg-dot.none{width:10px; height:2px; border-radius:1px; background:var(--rule)}

/* lists and tables elsewhere */
.panel{background:var(--surface); border:1px solid var(--rule); border-radius:10px}
table.list{border-collapse:collapse; width:100%}
table.list th, table.list td{padding:10px 14px; text-align:left; vertical-align:top; border-bottom:1px solid var(--rule)}
table.list tr:last-child td, table.list tr:last-child th{border-bottom:0}
table.list thead th{font-size:13px; color:var(--muted); font-weight:600}
.state{display:inline-flex; align-items:center; gap:6px; font-size:13px; font-weight:550}
.state::before{content:""; width:8px; height:8px; border-radius:50%; background:currentColor}
.state.ok{color:var(--ok)} .state.warn{color:var(--drift)} .state.bad{color:var(--danger)} .state.off{color:var(--muted)}
.conflict{padding:16px 18px; border-bottom:1px solid var(--rule)}
.conflict:last-child{border-bottom:0}
.conflict .key{font-family:var(--mono); font-size:14px; font-weight:600}
.choice{display:grid; grid-template-columns:minmax(90px,160px) minmax(0,1fr) auto; gap:4px 14px; align-items:center; margin-top:10px}
.choice .val{font-family:var(--mono); font-size:13px; background:var(--sunk); border-radius:5px; padding:4px 8px; overflow-wrap:anywhere}
.wins{font-size:12px; color:var(--ok); font-weight:600}
.empty{padding:28px 18px; color:var(--muted)}

.form-row{display:flex; gap:10px; flex-wrap:wrap; align-items:center}
input[type=text],input[type=password],input[type=time],select{border:1px solid var(--rule); background:var(--surface); border-radius:7px; padding:7px 10px; min-width:0}
input[type=text]{width:240px}
.checks{display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:6px 18px; padding:14px 18px}
.checks label{display:flex; gap:8px; align-items:center; font-family:var(--mono); font-size:13.5px}
.chips{display:flex; flex-wrap:wrap; gap:6px; padding:12px 18px}
.chip{display:inline-flex; align-items:center; gap:6px; font-family:var(--mono); font-size:13px; background:var(--sunk); border-radius:6px; padding:3px 4px 3px 9px}
.chip button{border:0; background:none; cursor:pointer; color:var(--muted); padding:0 5px; font-size:15px; line-height:1}
.chip button:hover{color:var(--danger)}
.addrow{padding:0 18px 14px}
.savebar{display:flex; gap:12px; align-items:center; margin-top:18px}
.note{font-size:13px; color:var(--muted)}
.note.ok{color:var(--ok)} .note.bad{color:var(--danger)}
.setting{display:grid; grid-template-columns:minmax(0,1fr) auto; gap:14px; align-items:center; padding:16px 18px; border-bottom:1px solid var(--rule)}
.setting:last-child{border-bottom:0}
.setting p{margin:2px 0 0; color:var(--muted); font-size:14px}

/* output drawer */
.drawer{position:fixed; left:0; right:0; bottom:0; background:var(--surface); border-top:1px solid var(--rule); box-shadow:0 -8px 24px rgba(15,20,25,.12); max-height:55vh; display:flex; flex-direction:column; z-index:20}
.drawer[hidden]{display:none}
.drawer header{display:flex; align-items:center; gap:12px; padding:12px 28px; border-bottom:1px solid var(--rule)}
.drawer header strong{font-size:15px}
.drawer pre{margin:0; padding:14px 28px 22px; overflow:auto; font:12.5px/1.55 var(--mono); white-space:pre-wrap; overflow-wrap:anywhere}
.spin{width:12px; height:12px; border-radius:50%; border:2px solid var(--rule); border-top-color:var(--ok); animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

dialog{border:1px solid var(--rule); border-radius:12px; background:var(--surface); color:var(--ink); padding:22px 24px; max-width:440px; width:calc(100% - 32px)}
dialog::backdrop{background:rgba(15,20,25,.45)}
dialog p{color:var(--muted); margin:6px 0 18px}
dialog .row{display:flex; justify-content:flex-end; gap:8px}

footer{max-width:1280px; margin:0 auto; padding:0 28px 32px 268px; display:flex; flex-wrap:wrap; gap:6px 20px; font-size:13px; color:var(--muted)}
footer .grow{flex:1}
footer button{border:0; background:none; padding:0; cursor:pointer; color:var(--muted); text-decoration:underline; text-underline-offset:3px}
.notice{background:var(--drift-soft); border-radius:10px; padding:14px 18px; margin:0 0 22px}

@media (max-width: 860px){
  .top{padding:14px 16px}
  .actions{margin-left:0; width:100%}
  .actions .btn{flex:1}
  .shell{grid-template-columns:1fr}
  nav.tabs{position:static; display:flex; overflow-x:auto; padding:10px 16px 0; gap:4px}
  nav.tabs a{white-space:nowrap}
  nav.tabs a[aria-current="page"]{box-shadow:inset 0 -3px 0 var(--ok)}
  main{padding:18px 16px 120px}
  h1{font-size:22px}
  footer{padding:0 16px 28px}
  .choice{grid-template-columns:1fr auto}
  .choice .acct{grid-column:1 / -1; margin-top:6px}
  .drawer header,.drawer pre{padding-left:16px; padding-right:16px}
  input[type=text]{width:100%}
}
@media (prefers-reduced-motion: reduce){ .spin{animation:none} }
@media (max-width: 860px){
  table.list.stack thead{display:none}
  table.list.stack tr{display:block; padding:12px 16px; border-bottom:1px solid var(--rule)}
  table.list.stack tr:last-child{border-bottom:0}
  table.list.stack td{display:block; border:0; padding:3px 0; text-align:left !important}
  table.list.stack td .btn{margin-top:6px}
}
.fp{overflow-wrap:anywhere}
</style>
</head>
<body>
<div class="top">
  <div class="brand">
    <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true"><circle cx="9" cy="9" r="6" fill="var(--ok)"/><circle cx="23" cy="9" r="6" fill="var(--ok)"/><circle cx="9" cy="23" r="6" fill="var(--ok)"/><circle cx="23" cy="23" r="5" fill="none" stroke="var(--drift)" stroke-width="2.5"/></svg>
    Account Sync
  </div>
  <div class="where" id="where"></div>
  <div class="actions">
    <button class="btn" id="previewBtn" type="button">Preview changes</button>
    <button class="btn primary" id="syncBtn" type="button">Sync now</button>
  </div>
</div>

<div class="shell">
  <nav class="tabs" aria-label="Sections">
    <a href="#overview">Overview</a>
    <a href="#differences">Settings that differ <span class="count" id="c-diff" hidden></span></a>
    <a href="#what">What to sync</a>
    <a href="#keys">API keys <span class="count" id="c-keys" hidden></span></a>
    <a href="#computers">Computers <span class="count" id="c-comp" hidden></span></a>
    <a href="#automation">Automation</a>
  </nav>
  <main id="main">
    <p class="muted">Loading your accounts…</p>
  </main>
</div>

<footer id="footer">
  <span class="grow" id="ver"></span>
  <span>Made by <a href="https://shorconsulting.com" target="_blank" rel="noopener noreferrer">David Shor</a></span>
  <a href="https://github.com/shorconsulting/claude-code-account-sync#readme" target="_blank" rel="noopener noreferrer">Help</a>
  <a href="https://github.com/shorconsulting/claude-code-account-sync/issues" target="_blank" rel="noopener noreferrer">Report a problem</a>
  <button type="button" id="quitBtn">Stop the settings page</button>
</footer>

<div class="drawer" id="drawer" hidden role="region" aria-label="Output">
  <header><span class="spin" id="spin"></span><strong id="drawerTitle"></strong><span class="note" id="drawerNote"></span>
    <span style="flex:1"></span><button class="btn small" type="button" id="drawerClose">Close</button></header>
  <pre id="drawerOut"></pre>
</div>

<dialog id="confirm"><h2 id="cTitle"></h2><p id="cBody"></p>
  <div class="row"><button class="btn" type="button" value="no" id="cNo">Cancel</button><button class="btn primary" type="button" id="cYes"></button></div></dialog>

<script>
"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let S = null;            // last state from the server
let draft = null;        // unsaved edits on the "What to sync" tab

async function api(path, body) {
  const opt = {headers: {"X-Page-Token": TOKEN}};
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(data.error || ("The page couldn't reach account-sync (" + r.status + "). Is the terminal window still open?"));
  return data;
}

function ask(title, body, yes) {
  return new Promise(res => {
    const d = $("#confirm");
    $("#cTitle").textContent = title; $("#cBody").textContent = body; $("#cYes").textContent = yes;
    const done = v => { d.close(); $("#cYes").onclick = $("#cNo").onclick = null; res(v); };
    $("#cYes").onclick = () => done(true); $("#cNo").onclick = () => done(false);
    d.oncancel = () => done(false);
    d.showModal();
  });
}

const plural = (n, one, many) => n + " " + (n === 1 ? one : (many || one + "s"));
function ago(iso) {
  if (!iso) return null;
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " minutes ago";
  if (s < 129600) return Math.round(s / 3600) + " hours ago";
  return Math.round(s / 86400) + " days ago";
}
const daysSince = iso => iso ? (Date.now() - new Date(iso).getTime()) / 86400000 : Infinity;

const DOT = {current: "Up to date", older: "Older version", missing: "Missing", manual: "Missing; add it on claude.ai", unknown: "Not checked yet", none: "Not shared through your sync repo"};
const dot = (st, what, where) => `<span class="peg-dot ${st}" role="img" aria-label="${esc(what)}: ${esc(DOT[st] || st)} in ${esc(where)}" title="${esc(DOT[st] || st)}"></span>`;

/* ---------- sections ---------- */

function overview() {
  const behind = S.accounts.filter(a => S.missing[a.name] > 0);
  let head;
  if (!S.groups.length) head = "Nothing to sync yet.";
  else if (!behind.length) head = `All ${plural(S.accounts.length, "account")} on this computer match.`;
  else {
    behind.sort((a, b) => S.missing[b.name] - S.missing[a.name]);
    const parts = behind.map((a, i) => i === 0 ? `${a.name} is behind on ${plural(S.missing[a.name], "item")}` : `${a.name} on ${S.missing[a.name]}`);
    head = (parts.length > 1 ? parts.slice(0, -1).join(", ") + " and " + parts.at(-1) : parts[0]) + ".";
    head = head[0].toUpperCase() + head.slice(1);
  }
  const cols = S.accounts.map(a => `<th class="acct" scope="col">${esc(a.name)}<small>${esc(a.folder)}</small></th>`).join("");
  let body = "";
  for (const g of S.groups) {
    body += `<tr class="group"><th colspan="${S.accounts.length + 2}" scope="rowgroup">${esc(g.title)} <span class="badge">${g.rows.length}</span></th></tr>`;
    for (const r of g.rows) {
      body += `<tr class="item"><th scope="row" title="${esc(r.path)}">${esc(r.name)}</th>` +
        S.accounts.map(a => `<td>${dot(r.cells[a.name] || "unknown", r.name, a.name)}</td>`).join("") +
        `<td class="hubcol">${dot(r.hub, r.name, "your sync repo")}</td></tr>`;
    }
  }
  const manualNote = S.groups.some(g => g.title === "claude.ai connectors")
    ? `<p class="note" style="margin-top:12px">claude.ai connectors belong to each claude.ai account, so a sync can't add them. Sign in to that account and add them at <a href="https://claude.ai/customize/connectors" target="_blank" rel="noopener noreferrer">claude.ai/customize/connectors</a>.</p>` : "";
  return `<h1>${esc(head)}</h1>
    <p class="lede">Each row is something Account Sync shares; each column is a Claude Code account on ${esc(S.machine)}. The last column is your sync repo, which carries everything between computers. Preview changes shows exactly what a sync would do. Nothing changes until you select Sync now.</p>
    ${S.groups.length ? `<div class="legend" aria-hidden="true">
      <span><span class="peg-dot current"></span>Up to date</span><span><span class="peg-dot older"></span>Older version</span>
      <span><span class="peg-dot missing"></span>Missing</span><span><span class="peg-dot manual"></span>Add on claude.ai</span></div>
    <div class="board"><table class="peg"><thead><tr><th scope="col">Item</th>${cols}<th class="acct hubcol" scope="col">Sync repo<small>all computers</small></th></tr></thead>
    <tbody>${body}</tbody></table></div>${manualNote}`
    : `<div class="panel empty">No skills, agents, commands, MCP servers or plugins found yet. Add something to any account, then come back.</div>`}`;
}

function differences() {
  const c = S.conflicts;
  const items = c.map((x, i) => `<div class="conflict"><div class="key">${esc(x.label)}</div>
    ${x.values.map(v => `<div class="choice"><span class="acct">${esc(v.account)}${v.wins ? ' <span class="wins">used now</span>' : ""}</span>
      <span class="val">${esc(v.value)}</span>
      <button class="btn small" type="button" data-resolve="${i}" data-acct="${esc(v.account)}">Use everywhere</button></div>`).join("")}
    </div>`).join("");
  return `<h1>${c.length ? plural(c.length, "setting differs", "settings differ") + " between accounts." : "Every account has the same settings."}</h1>
    <p class="lede">When accounts disagree, each sync keeps the most recently edited value${S.config.primary ? ", unless " + esc(S.config.primary) + " has one (it's your preferred account)" : ""}. Choose a value to give every account the same one now. Each account's settings.json is backed up first.</p>
    ${c.length ? `<div class="panel">${items}</div>` : ""}`;
}

function what() {
  const d = draft;
  const all = [...new Set([...S.config.default_sync_items, ...d.sync_items])];
  const chips = (field, ph, help) => `<div class="panel">
      <div class="chips">${d[field].length ? d[field].map(k => `<span class="chip">${esc(k)}<button type="button" aria-label="Remove ${esc(k)}" data-unchip="${field}" data-v="${esc(k)}">×</button></span>`).join("") : '<span class="note">None</span>'}</div>
      <div class="addrow form-row"><input type="text" placeholder="${esc(ph)}" aria-label="${esc(help)}" data-chipin="${field}"><button class="btn small" type="button" data-chipadd="${field}">Add</button></div></div>`;
  return `<h1>What to sync</h1>
    <p class="lede">These choices apply to this computer. Changes take effect at the next sync.</p>
    <h3>Folders and files copied between accounts</h3>
    <div class="panel"><div class="checks">${all.map(it => `<label><input type="checkbox" data-item="${esc(it)}" ${d.sync_items.includes(it) ? "checked" : ""}> ${esc(it)}</label>`).join("")}</div>
      <div class="addrow form-row"><input type="text" placeholder="folder or file name" aria-label="Add a folder or file to sync" id="newItem"><button class="btn small" type="button" id="addItem">Add</button></div></div>
    <h3>Settings that stay with each account</h3>
    <p class="note">These settings.json keys are tied to one account's sign-in, so they're never copied.</p>
    ${chips("settings_exclude", "settings key, e.g. apiKeyHelper", "Add a setting to keep per account")}
    <h3>Environment variables that stay with each account</h3>
    ${chips("env_exclude", "variable name", "Add an environment variable to keep per account")}
    <h3>When accounts disagree</h3>
    <div class="panel"><div class="setting"><div><strong>Preferred account</strong><p>Its value wins every conflict. Otherwise the newest edit wins.</p></div>
      <select id="primary" aria-label="Preferred account"><option value="">Newest edit wins</option>${S.accounts.map(a => `<option ${d.primary === a.name ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></div>
    <div class="setting"><div><strong>Check claude.ai connectors</strong><p>Lists each account's claude.ai connectors so you can see which are missing. Makes each sync a little slower.</p></div>
      <input type="checkbox" id="conn" aria-label="Check claude.ai connectors" ${d.inventory_connectors ? "checked" : ""}></div></div>
    <div class="savebar"><button class="btn primary" type="button" id="saveCfg">Save changes</button><span class="note" id="cfgNote"></span></div>`;
}

function keys() {
  const k = S.secrets;
  const missing = k.filter(s => s.used && !s.set).length;
  const rows = k.map(s => `<tr><td><strong>${esc(s.label)}</strong><div class="mono muted">${esc(s.name)}</div></td>
      <td>${s.set ? '<span class="state ok">Saved on this computer</span>' : (s.used ? '<span class="state warn">Missing on this computer</span>' : '<span class="state off">Not used anymore</span>')}</td>
      <td><div class="form-row"><input type="password" autocomplete="off" placeholder="${s.set ? "Replace value" : "Paste value"}" aria-label="Value for ${esc(s.label)}" data-secret="${esc(s.name)}"><button class="btn small" type="button" data-savesecret="${esc(s.name)}">Save</button></div></td></tr>`).join("");
  return `<h1>${missing ? plural(missing, "API key is", "API keys are") + " missing on this computer." : "API keys"}</h1>
    <p class="lede">Account Sync removes tokens and API keys before anything reaches your sync repo, and each computer keeps its own copy. Paste a missing value here and the next sync puts it into every account on this computer. Saved values are never shown again.</p>
    ${k.length ? `<div class="panel" style="overflow-x:auto"><table class="list stack"><thead><tr><th>Key</th><th>Status</th><th>Value</th></tr></thead><tbody>${rows}</tbody></table></div><p class="note" id="keyNote"></p>`
      : `<div class="panel empty">No keys or tokens found in your settings or MCP servers.</div>`}`;
}

function computers() {
  const stale = S.machines.filter(m => daysSince(m.last) > 14);
  const rows = S.machines.map(m => `<tr><td><strong>${esc(m.name)}</strong>${m.this ? ' <span class="badge">this computer</span>' : ""}</td>
      <td>${m.accounts.map(esc).join(", ") || "—"}</td>
      <td>${m.last ? (daysSince(m.last) > 14 ? `<span class="state warn">${esc(ago(m.last))}</span>` : esc(ago(m.last))) : '<span class="state off">Never synced</span>'}</td></tr>`).join("");
  return `<h1>${stale.length ? plural(stale.length, "computer hasn't", "computers haven't") + " synced in over two weeks." : "Computers sharing this setup"}</h1>
    <p class="lede">${S.config.remote ? "Every computer that syncs with your repo appears here." : "This computer isn't connected to a sync repo yet, so only its own accounts stay in step. To share with other computers, run <code>account-sync init --remote &lt;private repo URL&gt;</code>."}</p>
    <div class="panel" style="overflow-x:auto"><table class="list stack"><thead><tr><th>Computer</th><th>Accounts</th><th>Last sync</th></tr></thead><tbody>${rows}</tbody></table></div>
    <h3>Where the shared copy lives</h3>
    <div class="panel"><div class="setting"><div><strong>On this computer</strong><p class="mono">${esc(S.config.hub)}</p></div></div>
    <div class="setting"><div><strong>Sync repo</strong><p class="mono">${esc(S.config.remote || "Not set up")}</p></div></div></div>`;
}

function automation() {
  const sc = S.schedule;
  const st = sc.on ? `<span class="state ok">On${sc.time ? ", daily at " + esc(sc.time) : ""}</span>` : '<span class="state off">Off</span>';
  return `<h1>Automation</h1>
    <p class="lede">Account Sync can keep every account in step without you opening this page.</p>
    <div class="panel">
      <div class="setting"><div><strong>Daily sync</strong> ${st}<p>Runs once a day through ${esc(sc.how || "your computer's scheduler")}. A run missed while the computer was asleep happens when it wakes.</p></div>
        <div class="form-row"><input type="time" id="dailyTime" value="${esc(sc.time || "09:00")}" aria-label="Daily sync time">
          <button class="btn small" type="button" id="schedOn">${sc.on ? "Change time" : "Turn on"}</button>
          ${sc.on ? '<button class="btn small" type="button" id="schedOff">Turn off</button>' : ""}</div></div>
      <div class="setting"><div><strong>Sync when Claude Code starts</strong> ${S.hook ? '<span class="state ok">On</span>' : '<span class="state off">Off</span>'}<p>Each time you open Claude Code, a quick sync runs in the background, at most every 30 minutes.</p></div>
        <button class="btn small" type="button" id="hookBtn">${S.hook ? "Turn off" : "Turn on"}</button></div>
    </div>`;
}

const SECTIONS = {overview, differences, what, keys, computers, automation};

function render() {
  const id = (location.hash.slice(1) in SECTIONS) ? location.hash.slice(1) : "overview";
  document.querySelectorAll("nav.tabs a").forEach(a => { if (a.hash !== "#" + id) a.removeAttribute("aria-current"); else a.setAttribute("aria-current", "page"); });
  if (!S) return;
  $("#main").innerHTML = SECTIONS[id]();
  wire(id);
}

function setCount(sel, n) { const el = $(sel); el.hidden = !n; el.textContent = n || ""; }

async function load() {
  try {
    S = await api("/api/state");
  } catch (e) {
    $("#main").innerHTML = `<div class="notice">${esc(e.message)}</div>`; return;
  }
  if (S.setup_needed) {
    $("#main").innerHTML = `<h1>Account Sync isn't set up on this computer yet.</h1><p class="lede">Run <code>account-sync init</code> in a terminal, then reload this page.</p>`;
    return;
  }
  draft = JSON.parse(JSON.stringify({sync_items: S.config.sync_items, settings_exclude: S.config.settings_exclude,
    env_exclude: S.config.env_exclude, primary: S.config.primary || "", inventory_connectors: S.config.inventory_connectors}));
  $("#where").textContent = `${S.accounts.length} accounts on ${S.machine}`;
  $("#ver").textContent = `Account Sync ${S.version}`;
  setCount("#c-diff", S.conflicts.length);
  setCount("#c-keys", S.secrets.filter(s => s.used && !s.set).length);
  setCount("#c-comp", S.machines.filter(m => daysSince(m.last) > 14).length);
  render();
}

/* ---------- actions ---------- */

function wire(id) {
  if (id === "differences") {
    document.querySelectorAll("[data-resolve]").forEach(b => b.onclick = async () => {
      const c = S.conflicts[+b.dataset.resolve];
      if (!await ask(`Use ${b.dataset.acct}'s value everywhere?`, `Every account on this computer gets ${b.dataset.acct}'s ${c.label}. Each settings.json is backed up first.`, "Use everywhere")) return;
      try { await api("/api/resolve", {keys: c.keys, account: b.dataset.acct}); await load(); }
      catch (e) { await ask("That didn't work", e.message, "OK"); }
    });
  }
  if (id === "what") {
    document.querySelectorAll("[data-item]").forEach(cb => cb.onchange = () => {
      const v = cb.dataset.item;
      draft.sync_items = cb.checked ? [...draft.sync_items, v] : draft.sync_items.filter(x => x !== v);
    });
    $("#addItem").onclick = () => { const v = $("#newItem").value.trim(); if (v && !draft.sync_items.includes(v)) draft.sync_items.push(v); render(); };
    document.querySelectorAll("[data-unchip]").forEach(b => b.onclick = () => { const f = b.dataset.unchip; draft[f] = draft[f].filter(x => x !== b.dataset.v); render(); });
    document.querySelectorAll("[data-chipadd]").forEach(b => b.onclick = () => {
      const f = b.dataset.chipadd, inp = $(`[data-chipin="${f}"]`), v = inp.value.trim();
      if (v && !draft[f].includes(v)) draft[f].push(v); render();
    });
    $("#primary").onchange = e => draft.primary = e.target.value;
    $("#conn").onchange = e => draft.inventory_connectors = e.target.checked;
    $("#saveCfg").onclick = async () => {
      const note = $("#cfgNote");
      try { await api("/api/config", {...draft, primary: draft.primary || null}); await load(); location.hash = "#what"; $("#cfgNote").className = "note ok"; $("#cfgNote").textContent = "Saved. Changes take effect at the next sync."; }
      catch (e) { note.className = "note bad"; note.textContent = e.message; }
    };
  }
  if (id === "keys") {
    document.querySelectorAll("[data-savesecret]").forEach(b => b.onclick = async () => {
      const name = b.dataset.savesecret, inp = $(`[data-secret="${name}"]`);
      try { await api("/api/secret", {name, value: inp.value}); inp.value = ""; await load(); $("#keyNote").className = "note ok"; $("#keyNote").textContent = "Saved. The next sync puts it into every account on this computer."; }
      catch (e) { $("#keyNote").className = "note bad"; $("#keyNote").textContent = e.message; }
    });
  }
  if (id === "automation") {
    $("#schedOn").onclick = () => run("schedule", "Daily sync", {time: $("#dailyTime").value});
    if ($("#schedOff")) $("#schedOff").onclick = () => run("schedule", "Daily sync", {off: true});
    $("#hookBtn").onclick = () => run("hook", "Sync when Claude Code starts", {off: S.hook});
  }
}

let polling = null;
async function run(action, title, body = {}) {
  const d = $("#drawer");
  d.hidden = false; $("#drawerTitle").textContent = title; $("#drawerOut").textContent = ""; $("#spin").hidden = false;
  $("#drawerNote").className = "note"; $("#drawerNote").textContent = "Running…";
  $("#previewBtn").disabled = $("#syncBtn").disabled = true;
  try { await api("/api/run/" + action, body); }
  catch (e) { $("#drawerOut").textContent = e.message; finish(1); return; }
  clearInterval(polling);
  polling = setInterval(async () => {
    let j; try { j = await api("/api/job"); } catch (e) { return; }
    const out = $("#drawerOut"), atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 20;
    out.textContent = j.output || "";
    if (atEnd) out.scrollTop = out.scrollHeight;
    if (!j.running) { clearInterval(polling); finish(j.code); load(); }
  }, 700);
}
function finish(code) {
  $("#spin").hidden = true;
  $("#drawerNote").className = "note " + (code === 0 ? "ok" : "bad");
  $("#drawerNote").textContent = code === 0 ? "Finished" : "Finished with problems; see the lines starting with !";
  $("#previewBtn").disabled = $("#syncBtn").disabled = false;
}

$("#previewBtn").onclick = () => run("preview", "Preview: what a sync would change");
$("#syncBtn").onclick = async () => {
  if (!S) return;
  if (await ask(`Sync ${plural(S.accounts.length, "account")} now?`, "Every account on this computer gets everything the others have, and your sync repo is updated. Everything that changes is backed up first.", "Sync now"))
    run("sync", "Sync");
};
$("#drawerClose").onclick = () => $("#drawer").hidden = true;
$("#quitBtn").onclick = async () => {
  try { await api("/api/quit", {}); } catch (e) { await ask("Not yet", e.message, "OK"); return; }
  document.body.innerHTML = '<main style="padding:40px"><h1>The settings page has stopped.</h1><p class="lede">You can close this tab. Run <code>account-sync ui</code> to open it again.</p></main>';
};
window.addEventListener("hashchange", render);
setInterval(() => { if (!document.hidden) api("/api/ping").catch(() => {}); }, 60000);
load();
</script>
</body>
</html>
"""


class LocalPage:
    """A token-locked page + JSON API on 127.0.0.1. Shared shape with the other tool's settings page."""

    def __init__(self, title, html, get_state, posts, job_argv, script, port=0):
        import secrets as _s
        import threading
        self.title, self.get_state, self.posts, self.job_argv = title, get_state, posts, job_argv
        self.script = script
        self.token = _s.token_urlsafe(24)
        self.nonce = _s.token_urlsafe(16)
        self.html = html.replace("<script>", f'<script nonce="{self.nonce}">')
        self.job = None
        self.job_lock = threading.Lock()
        self.last_seen = time.time()
        self.port = port

    def run_job(self, argv):
        import threading
        with self.job_lock:
            if self.job and self.job["running"]:
                raise ValueError("another action is still running")
            job = {"id": int(time.time() * 1000), "argv": argv, "running": True, "output": "", "code": None}
            self.job = job
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", PYTHONUNBUFFERED="1")
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        # own process group: Ctrl+C or closing the page never interrupts a sync halfway,
        # and with no terminal attached nothing can sit waiting for a password prompt
        detach = {"creationflags": 0x00000200} if IS_WINDOWS else {"start_new_session": True}

        def work():
            try:
                p = subprocess.Popen([sys.executable, str(self.script)] + argv, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", env=env, **detach)
                for line in p.stdout:
                    job["output"] += line
                job["code"] = p.wait()
            except Exception as e:                       # report, never crash the page
                job["output"] += f"\n{e}\n"
                job["code"] = 1
            job["running"] = False
        threading.Thread(target=work, daemon=True).start()
        return job

    def busy(self):
        return bool(self.job and self.job["running"])

    def _open_browser(self, url):
        """Open the page without putting the token on a command line other users can read."""
        import webbrowser
        if IS_MAC or IS_WINDOWS:                 # handed over via Apple Events / ShellExecute, not argv
            webbrowser.open(url)
            return
        import tempfile
        import threading
        # in the home folder: browsers installed as snaps can't read /tmp
        fd, path = tempfile.mkstemp(prefix="settings-page-", suffix=".html", dir=str(HOME))
        with os.fdopen(fd, "w", encoding="utf-8") as f:   # mkstemp: readable by this user only
            f.write(f'<!doctype html><meta http-equiv="refresh" content="0;url={url}"><a href="{url}">Open</a>')
        webbrowser.open("file://" + path)
        t = threading.Timer(60, lambda: os.path.exists(path) and os.unlink(path))
        t.daemon = True
        t.start()

    def serve(self, open_browser=True):
        import hmac
        import http.server
        import threading
        import urllib.parse
        page = self

        def same(a, b):
            return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "local"
            sys_version = ""
            timeout = 30                                  # idle connections don't pin threads

            def log_message(self, *a):
                pass

            def _host_ok(self):
                return self.headers.get("Host", "") in (f"127.0.0.1:{page.port}", f"localhost:{page.port}")

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else (
                    json.dumps(body).encode() if ctype == "application/json" else body.encode())
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 f"default-src 'none'; script-src 'nonce-{page.nonce}'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
                                 "frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                ok = self._host_ok() and same(self.headers.get("X-Page-Token", ""), page.token)
                if ok:
                    page.last_seen = time.time()
                return ok

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                if u.path == "/":
                    q = urllib.parse.parse_qs(u.query).get("t", [""])[0]
                    if not (self._host_ok() and same(q, page.token)):
                        return self._send(403, "<p>Open the link printed in your terminal. It changes every time "
                                               "the settings page starts.</p>", "text/html")
                    page.last_seen = time.time()
                    return self._send(200, page.html, "text/html")
                if not self._authed():
                    return self._send(403, {"error": "not allowed"})
                try:
                    if u.path == "/api/state":
                        return self._send(200, page.get_state())
                    if u.path == "/api/job":
                        return self._send(200, page.job or {})
                    if u.path == "/api/ping":
                        return self._send(200, {"ok": True})
                except SystemExit as e:
                    return self._send(200, {"setup_needed": str(e)})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._authed() or not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self._send(403, {"error": "not allowed"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 1_000_000:
                        raise ValueError("request too large")
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("bad request")
                    path = urllib.parse.urlparse(self.path).path
                    if path == "/api/quit":
                        if page.busy():
                            raise ValueError("wait for the running action to finish, then stop the page")
                        self._send(200, {"ok": True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                        return
                    if path.startswith("/api/run/"):
                        job = page.run_job(page.job_argv(path[len("/api/run/"):], body))
                        return self._send(200, job)
                    fn = page.posts.get(path)
                    if not fn:
                        return self._send(404, {"error": "not found"})
                    return self._send(200, fn(body) or {"ok": True})
                except (ValueError, KeyError, TypeError) as e:
                    return self._send(400, {"error": str(e)})
                except SystemExit as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(500, {"error": str(e)})

        class Server(http.server.ThreadingHTTPServer):
            allow_reuse_address = not IS_WINDOWS          # on Windows it would let another user share the port
            daemon_threads = True

        srv = Server(("127.0.0.1", self.port), Handler)
        self.port = srv.server_address[1]
        url = f"http://127.0.0.1:{self.port}/?t={self.token}"

        def idle_watch():                          # stop on its own once the tab has been closed a while
            while True:
                time.sleep(30)
                if not self.busy() and time.time() - self.last_seen > 1800:
                    srv.shutdown()
                    return
        threading.Thread(target=idle_watch, daemon=True).start()
        print(f"{self.title} is open at:\n  {url}\n"
              f"Only this link works; it changes every time. Leave this window open while you use the page,\n"
              f"press Ctrl+C to stop (it also stops by itself 30 minutes after you close the tab).", flush=True)
        if open_browser:
            try:
                self._open_browser(url)
            except Exception:
                pass
        try:
            srv.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        if self.busy():        # the action runs in its own process group, so Ctrl+C didn't reach it
            print("Letting the running action finish first, so nothing is left half done...", flush=True)
            try:
                while self.busy():
                    time.sleep(0.3)
                print((self.job or {}).get("output", "")[-2000:])
            except KeyboardInterrupt:
                print("Stopped waiting. The action may be left half done; run it again to finish.")
        print("Settings page stopped.")


def _ui_locked(fn):
    def wrapped(body):
        with Lock() as lk:
            if not lk.held:
                raise ValueError("a sync is running; try again in a moment")
            return fn(body)
    return wrapped


def cmd_ui(args):
    load_config()
    posts = {
        "/api/config": _ui_locked(lambda b: ui_save_config(b)),
        "/api/secret": _ui_locked(lambda b: ui_set_secret(b.get("name"), b.get("value"))),
        "/api/resolve": _ui_locked(lambda b: {"changed": ui_resolve(load_config(), b.get("keys"), b.get("account"))}),
    }
    LocalPage("The Account Sync settings page", ACCOUNT_SYNC_UI, ui_state, posts, ui_job_argv,
              Path(__file__).resolve(), port=args.port).serve(open_browser=not args.no_browser)


def main(argv=None):
    p = argparse.ArgumentParser(prog="account_sync.py", description="Keep all Claude Code accounts identical (union of everything).")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, apply_flags=True):
        sp.add_argument("--no-git", action="store_true", help="don't pull/push the hub repo")
        sp.add_argument("--quiet", action="store_true")
        if apply_flags:
            sp.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
            sp.add_argument("--account", action="append", help="only apply to this account (repeatable)")
            sp.add_argument("--force", action="store_true", help="overwrite differing MCP servers and newer local files")
            sp.add_argument("--prune", action="store_true", help="make accounts mirror the hub exactly (removes extras; backed up)")
            sp.add_argument("--direct", action="store_true", help="write MCP servers into .claude.json instead of using the CLI")

    sp = sub.add_parser("discover", help="list Claude Code config dirs found on this machine")
    sp.set_defaults(fn=cmd_discover)

    sp = sub.add_parser("init", help="set up ~/.account-sync and register accounts")
    sp.add_argument("--hub", help="hub folder (default ~/.account-sync/hub)")
    sp.add_argument("--remote", help="private git repo URL to share the hub across machines")
    sp.add_argument("--add", action="append", help="extra config dir to register (repeatable)")
    sp.add_argument("--primary", help="account whose values win conflicts by default")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("accounts", help="add/remove/list registered accounts")
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("path", nargs="?")
    sp.add_argument("--name")
    sp.set_defaults(fn=cmd_accounts)

    sp = sub.add_parser("harvest", help="union every account into the hub")
    common(sp, apply_flags=False)
    sp.add_argument("--prefer", help="this account wins every conflict")
    sp.add_argument("--skip-connectors", action="store_true", help="skip the claude.ai connector inventory (faster)")
    sp.set_defaults(fn=cmd_harvest)

    sp = sub.add_parser("apply", help="push the hub into every account")
    common(sp)
    sp.set_defaults(fn=cmd_apply)

    sp = sub.add_parser("sync", help="harvest + apply (+ git pull/push)")
    common(sp)
    sp.add_argument("--prefer", help="this account wins every conflict")
    sp.add_argument("--skip-connectors", action="store_true")
    sp.add_argument("--auto", action="store_true", help="skip if the last sync was recent (used by the hook)")
    sp.add_argument("--background", action="store_true", help="run detached and return immediately")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("status", help="dry-run sync: show what each account is missing")
    sp.add_argument("--account", action="append")
    sp.add_argument("--prefer")
    sp.add_argument("--skip-connectors", action="store_true")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_status, force=False, prune=False, direct=False, auto=False, background=False)

    sp = sub.add_parser("install-hook", help="run sync automatically whenever Claude Code starts")
    sp.add_argument("--remove", action="store_true")
    sp.set_defaults(fn=cmd_install_hook)

    sp = sub.add_parser("schedule", help="run sync on a timer (launchd / cron / Task Scheduler); daily at 09:00 by default")
    sp.add_argument("--daily", metavar="HH:MM", help="run once a day at this local time (default 09:00)")
    sp.add_argument("--every", type=int, help="instead of daily, run every N minutes (min 15)")
    sp.add_argument("--print", action="store_true", help="show what would be installed, install nothing")
    sp.add_argument("--remove", action="store_true")
    sp.set_defaults(fn=cmd_schedule)

    sp = sub.add_parser("ui", help="open the settings page in your browser")
    sp.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    sp.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")
    sp.set_defaults(fn=cmd_ui)

    args = p.parse_args(argv)
    Log.quiet = bool(getattr(args, "quiet", False))
    migrate_legacy_state()
    args.fn(args)


if __name__ == "__main__":
    main()
