#!/usr/bin/env python3
"""
Check Account Sync on this computer, end to end, without touching your own setup.

Built for Windows (run:  py -3 tests\\windows_check.py ), and it also runs on macOS and Linux.
It makes a throwaway home folder whose path contains spaces, three fake Claude Code accounts,
and a stand-in `claude` command (a .cmd file on Windows, like the real npm install), then runs
init, status, sync, the startup hook, the schedule and the settings page against them.

Nothing outside the throwaway folder changes. The one exception is --real-scheduler, which
creates a real Task Scheduler / launchd / cron entry for a moment and deletes it again
(skipped automatically if you already have an Account Sync schedule).

Results are printed and also saved to account-sync-test-results.txt in the current folder.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "account_sync.py"
WIN = os.name == "nt"
REAL_SCHEDULER = "--real-scheduler" in sys.argv
KEEP = "--keep" in sys.argv
lines, failures = [], []


def say(msg=""):
    print(msg, flush=True)
    lines.append(msg)


def check(name, ok, detail=""):
    say(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f"\n         {detail}"))
    if not ok:
        failures.append(name)


def skip(name, why):
    say(f"  skip {name} ({why})")


# ── a throwaway home with spaces in its path ──
root = Path(tempfile.mkdtemp(prefix="account sync test "))
home = root / "Test User"
fakebin = root / "fake bin"
for d in (home, fakebin, home / ".local" / "bin"):
    d.mkdir(parents=True)
env = dict(os.environ)
for k in ("CLAUDE_CONFIG_DIR", "ACCOUNT_SYNC_HOME"):
    env.pop(k, None)
env.update(HOME=str(home), USERPROFILE=str(home), ACCOUNT_SYNC_HOSTNAME="testpc",
           # the command folder is already on PATH, so init never edits your real PATH
           PATH=os.pathsep.join([str(fakebin), str(home / ".local" / "bin"), env.get("PATH", "")]))

# stand-in `claude` (Windows: a .cmd wrapper, exactly how npm installs the real one)
shutil.copy(HERE / "fake_claude", fakebin / "fake_claude.py")
if WIN:
    (fakebin / "claude.cmd").write_text(f'@"{sys.executable}" "{fakebin / "fake_claude.py"}" %*\r\n', encoding="utf-8")
else:
    (fakebin / "claude").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fakebin / "fake_claude.py"}" "$@"\n')
    os.chmod(fakebin / "claude", 0o755)


def w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def wj(path, obj):
    w(path, json.dumps(obj, indent=2))


def rj(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ── three accounts: a brand-new default one, "work" and "personal" ──
D, W, P = home / ".claude", home / ".claude-work", home / ".claude-personal"
wj(D / ".credentials.json", {"claudeAiOauth": {"accessToken": "SECRET-OAUTH"}})
wj(home / ".claude.json", {"oauthAccount": {"emailAddress": "new@example.com"}})
wj(D / "fake_state.json", {"marketplaces": {"claude-plugins-official": {"source": "github", "repo": "anthropics/claude-plugins-official"}},
                           "plugins": {}, "connectors": ["claude.ai Gmail"], "synced": []})
hook_cmd = f"powershell -File {W / 'hooks' / 'fmt.ps1'}" if WIN else f"{W}/hooks/fmt.sh"
wj(W / "settings.json", {
    "permissions": {"allow": ["Bash(npm run *)", "Bash(git diff *)"], "deny": ["Read(./.env)"]},
    "env": {"GITHUB_TOKEN": "ghp_workTOKEN1234567890", "NODE_ENV": "development"},
    "hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": hook_cmd}]}]},
    "apiKeyHelper": "work-key-helper", "theme": "dark",
    "enabledPlugins": {"formatter@team-market": True},
    "extraKnownMarketplaces": {"team-market": {"source": {"source": "github", "repo": "acme/team-market"}}}})
w(W / "skills" / "review" / "SKILL.md", "---\ndescription: Review code (WORK version, newer)\n---\nReview carefully.\n")
w(W / "agents" / "reviewer.md", "---\nname: reviewer\ndescription: reviews\n---\nYou review.\n")
w(W / "CLAUDE.md", "# Work rules\n- Use conventional commits\n")
w(W / "hooks" / "fmt.ps1", "Write-Output fmt\n")
wj(W / ".claude.json", {"oauthAccount": {"emailAddress": "work@example.com"}, "mcpServers": {
    "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ghp_mcpSECRETvalue123456"}},
    "sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"}}})
wj(W / "fake_state.json", {"marketplaces": {"claude-plugins-official": {"source": "github", "repo": "anthropics/claude-plugins-official"},
                                            "team-market": {"source": "github", "repo": "acme/team-market"}},
                           "plugins": {"formatter@team-market": True}, "connectors": ["claude.ai Slack"], "synced": []})
wj(P / "settings.json", {"permissions": {"allow": ["Bash(pytest *)"]}, "model": "opus", "theme": "light"})
w(P / "skills" / "review" / "SKILL.md", "---\ndescription: Review code (PERSONAL version, older)\n---\nReview.\n")
os.utime(P / "skills" / "review" / "SKILL.md", (time.time() - 86400 * 30,) * 2)
w(P / "skills" / "notes" / "SKILL.md", "---\ndescription: Take notes\n---\nNotes.\n")
w(P / "commands" / "fix.md", "Fix issue $ARGUMENTS\n")
wj(P / ".claude.json", {"mcpServers": {"db": {"type": "stdio", "command": "npx", "args": ["-y", "@bytebase/dbhub"],
                                              "env": {"DB_PASSWORD": "hunter2"}}}})
wj(P / "fake_state.json", {"marketplaces": {"claude-plugins-official": {"source": "github", "repo": "anthropics/claude-plugins-official"}},
                           "plugins": {}, "connectors": ["claude.ai Gmail"], "synced": []})
# PowerShell profile pointing at the personal account (discovery reads it)
w(home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
  '$env:CLAUDE_CONFIG_DIR = "$env:USERPROFILE\\.claude-personal"\n')


def run(*args, timeout=180):
    r = subprocess.run([sys.executable, str(SCRIPT)] + list(args), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout + r.stderr


say(f"Account Sync check on {platform.system()} {platform.release()} ({platform.machine()}), "
    f"Python {platform.python_version()}")
say(f"Throwaway folder: {root}")
say(f"git: {'found' if shutil.which('git') else 'not installed (sharing between computers is skipped)'}")
say()

say("== setup")
code, out = run("init")
check("init runs", code == 0, out[-600:])
cfg = rj(home / ".account-sync" / "config.json", {})
names = sorted(a["name"] for a in cfg.get("accounts", []))
check("finds all three accounts", names == ["default", "personal", "work"], f"found {names}")
shim = home / ".local" / "bin" / ("account-sync.cmd" if WIN else "account-sync")
check("adds the account-sync command", shim.exists())
if shim.exists():
    r = subprocess.run([str(shim), "--version"], env=env, capture_output=True, text=True)
    check("the account-sync command runs", r.returncode == 0 and r.stdout.strip().startswith("1."),
          (r.stdout + r.stderr)[-300:])
check("finds the stand-in claude", bool(cfg.get("claude_bin")), f"claude_bin = {cfg.get('claude_bin')}")

say("== preview and sync")
code, out = run("status")
check("status (preview) runs", code == 0, out[-600:])
check("status changes nothing", not (D / "settings.json").exists())
code, out = run("sync")
check("sync runs", code == 0, out[-800:])
check("new account got every skill", (D / "skills" / "review" / "SKILL.md").exists() and (D / "skills" / "notes" / "SKILL.md").exists())
check("newest version of a skill wins", "WORK version" in (P / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8"))
same_bytes = (D / "skills" / "review" / "SKILL.md").read_bytes() == (W / "skills" / "review" / "SKILL.md").read_bytes()
check("files copied byte for byte (no CRLF added)", same_bytes)
ds = rj(D / "settings.json", {})
allow = (ds.get("permissions") or {}).get("allow", [])
check("permissions merged from every account", "Bash(pytest *)" in allow and "Bash(git diff *)" in allow, str(allow))
check("account-only settings not copied", "apiKeyHelper" not in ds)
hook_cmds = [h.get("command", "") for g in (ds.get("hooks") or {}).get("PostToolUse", []) for h in g.get("hooks", [])]
want = str(D / "hooks" / ("fmt.ps1" if WIN else "fmt.sh"))
check("hook paths point at each account's own folder", any(want in c for c in hook_cmds), f"got {hook_cmds}, want {want}")
mcp = (rj(home / ".claude.json", {}) or {}).get("mcpServers", {})
check("MCP servers added (written directly, since claude is a .cmd)" if WIN else "MCP servers added",
      {"db", "github", "sentry"} <= set(mcp), f"got {sorted(mcp)}")
check("MCP token restored on this computer", "ghp_mcpSECRETvalue123456" in json.dumps(mcp.get("github", {})))
fs = rj(D / "fake_state.json", {})
check("plugins installed through claude", "formatter@team-market" in fs.get("plugins", {}), str(fs.get("plugins")))
hub = home / ".account-sync" / "hub"
leaked = [p.name for p in hub.rglob("*") if p.is_file() and ".git" not in p.parts and
          re.search(r"ghp_|hunter2|SECRET-OAUTH", p.read_text(encoding="utf-8", errors="ignore"))]
check("no tokens or passwords in the shared copy", not leaked, f"found in {leaked}")
calls = home / "claude_calls.log"
calls.write_text("")
code, out = run("sync", "--skip-connectors")
changed = [l for l in calls.read_text().splitlines() if " list" not in l]
check("second sync changes nothing", code == 0 and not changed, "; ".join(changed)[:300])

if shutil.which("git"):
    say("== git (sharing between computers)")
    r = subprocess.run(["git", "-C", str(hub), "log", "--oneline"], capture_output=True, text=True, env=env)
    check("the shared copy is committed to git", r.returncode == 0 and "sync from testpc" in r.stdout, r.stdout + r.stderr)
    check("line-ending conversion is off", "* -text" in (hub / ".gitattributes").read_text())
    r = subprocess.run(["git", "-C", str(hub), "ls-files", "--eol", "files/skills/review/SKILL.md"],
                       capture_output=True, text=True, env=env)
    check("skill stored with LF endings", "i/lf" in r.stdout and "w/lf" in r.stdout, r.stdout.strip())
else:
    skip("git checks", "git isn't installed")

say("== automation")
code, out = run("install-hook")
s = rj(W / "settings.json", {})
hooks = [h.get("command", "") for g in (s.get("hooks") or {}).get("SessionStart", []) for h in g.get("hooks", [])]
ok = code == 0 and any("sync --auto" in c for c in hooks)
check("startup hook installed", ok, out[-300:])
if WIN and hooks:
    check("startup hook uses forward slashes", all("\\" not in c for c in hooks if "sync --auto" in c), str(hooks))
run("install-hook", "--remove")
code, out = run("schedule", "--daily", "07:30", "--print")
if WIN:
    check("daily task renders for Task Scheduler", code == 0 and "/SC DAILY /ST 07:30" in out, out[-400:])
    check("daily task runs without a console window (pythonw)", "pythonw" in out.lower(), out[-400:])
elif sys.platform == "darwin":
    check("daily launchd job renders", code == 0 and "<integer>7</integer>" in out and "<integer>30</integer>" in out, out[-300:])
else:
    check("daily cron line renders", code == 0 and "30 7 * * *" in out, out[-300:])

if REAL_SCHEDULER:
    if WIN:
        exists = subprocess.run(["schtasks", "/Query", "/TN", "AccountSync"], capture_output=True).returncode == 0
        if exists:
            skip("real Task Scheduler entry", "you already have an AccountSync task, so it's left alone")
        else:
            code, out = run("schedule", "--daily", "07:30")
            q = subprocess.run(["schtasks", "/Query", "/TN", "AccountSync", "/FO", "LIST", "/V"], capture_output=True, text=True)
            check("real Task Scheduler entry created", code == 0 and q.returncode == 0, (out + q.stdout + q.stderr)[-500:])
            run("schedule", "--remove")
            gone = subprocess.run(["schtasks", "/Query", "/TN", "AccountSync"], capture_output=True).returncode != 0
            check("real Task Scheduler entry removed again", gone)
    else:
        skip("real scheduler", "only checked on Windows")
else:
    skip("real Task Scheduler entry", "add --real-scheduler to create one for a moment and delete it")

say("== settings page")
proc = subprocess.Popen([sys.executable, str(SCRIPT), "ui", "--no-browser"], env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
url, t0 = "", time.time()
while time.time() - t0 < 20:
    line = proc.stdout.readline()
    m = re.search(r"(http://127\.0\.0\.1:\d+)/\?t=(\S+)", line)
    if m:
        base, tok = m.group(1), m.group(2)
        url = line.strip()
        break
check("settings page starts", bool(url))
if url:
    def http(path, token=None, body=None):
        h = {"X-Page-Token": token} if token else {}
        data = None
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=h), timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    check("page refuses a missing token", http("/")[0] == 403)
    check("page opens with the token", http(f"/?t={tok}")[0] == 200)
    check("API refuses requests without the token", http("/api/state")[0] == 403)
    st, body = http("/api/state", tok)
    state = json.loads(body) if st == 200 else {}
    check("API lists every account", len(state.get("accounts", [])) == 3, body[:300])
    check("API never returns secret values", not re.search(r"ghp_|hunter2|SECRET-OAUTH", body))
    st, _ = http("/api/secret", tok, {"name": "CSYNC_TEST_KEY", "value": "v123"})
    check("API key saved from the page", st == 200 and "v123" in (home / ".account-sync" / "secrets.json").read_text())
    http("/api/quit", tok, {})
try:
    proc.wait(timeout=15)
    check("settings page stops", proc.returncode is not None)
except subprocess.TimeoutExpired:
    proc.kill()
    check("settings page stops", False, "had to be killed")

say()
say(f"{len(failures)} failed" + (": " + ", ".join(failures) if failures else ". Everything passed."))
out_file = Path.cwd() / "account-sync-test-results.txt"
try:
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved these results to {out_file}")
except OSError:
    pass
if KEEP:
    print(f"Kept the throwaway folder: {root}")
else:
    shutil.rmtree(root, ignore_errors=True)
sys.exit(1 if failures else 0)
