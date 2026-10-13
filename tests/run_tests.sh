#!/usr/bin/env bash
# End-to-end test: three fake accounts (one brand new) and a stub `claude` CLI.
# Nothing outside a temporary folder is touched. Run:  bash tests/run_tests.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../account_sync.py"
export CSYNC_TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$CSYNC_TEST_DIR"' EXIT
bash "$HERE/make_fake_home.sh" >/dev/null
export HOME="$CSYNC_TEST_DIR/home" PATH="$CSYNC_TEST_DIR/fakebin:$PATH" ACCOUNT_SYNC_HOSTNAME=testhost
unset CLAUDE_CONFIG_DIR || true
H="$HOME"
pass=0; fail=0
check() { if eval "$2"; then echo "  ok   $1"; pass=$((pass+1)); else echo "  FAIL $1"; fail=$((fail+1)); fi; }

python3 "$SCRIPT" init >/dev/null
python3 "$SCRIPT" status >/dev/null
check "dry run changes nothing"            '[ ! -e "$H/.claude/settings.json" ]'

python3 "$SCRIPT" sync --quiet
check "new account got skills"             '[ -f "$H/.claude/skills/review/SKILL.md" ] && [ -f "$H/.claude/skills/notes/SKILL.md" ]'
check "newest skill version won"           'grep -q "WORK version" "$H/.claude-personal/skills/review/SKILL.md"'
check "older version kept in conflicts"    'grep -rq "PERSONAL version" "$H/.account-sync/hub/conflicts"'
check "claude.ai-synced skill not copied"  '[ ! -e "$H/.claude/skills/synced" ]'
check "permissions unioned"                'grep -q "pytest" "$H/.claude-work/settings.json" && grep -q "git diff" "$H/.claude-personal/settings.json"'
check "account-only keys not spread"       '! grep -qE "apiKeyHelper|sk-ant" "$H/.claude/settings.json" && grep -q work-key "$H/.claude-work/settings.json"'
check "paths rewritten per account"        'grep -q "$H/.claude/hooks/fmt.sh" "$H/.claude/settings.json"'
check "MCP servers added"                  'python3 -c "import json,sys; s=json.load(open(\"$H/.claude.json\"))[\"mcpServers\"]; sys.exit(0 if {\"db\",\"github\",\"sentry\"}<=set(s) else 1)"'
check "plugins installed"                  'grep -q "formatter@team-market" "$H/.claude/fake_state.json"'
check "no secrets in hub"                  '! grep -rqE "ghp_|hunter2|supersecret|sk-ant|SECRET-OAUTH" "$H/.account-sync/hub"'
check "secrets file is private"            '[ "$(stat -c %a "$H/.account-sync/secrets.json" 2>/dev/null || stat -f %Lp "$H/.account-sync/secrets.json")" = 600 ]'

: > "$H/claude_calls.log"
python3 "$SCRIPT" sync --quiet --skip-connectors
check "second run is a no-op"              '! grep -v " list" "$H/claude_calls.log" | grep -q .'
if [ "$(uname)" = Darwin ]; then
  check "daily launchd job at 07:30"        'python3 "$SCRIPT" schedule --daily 07:30 --print | python3 -c "import plistlib,sys; c=plistlib.loads(sys.stdin.buffer.read())[\"StartCalendarInterval\"]; sys.exit(0 if (c[\"Hour\"], c[\"Minute\"]) == (7, 30) else 1)"'
else
  check "daily schedule renders"           'python3 "$SCRIPT" schedule --daily 07:30 --print | grep -q "30 7 \* \* \*"'
fi

echo "== cross-platform"
HUB="$H/.account-sync/hub"; OSF="$HUB/settings.$(python3 -c 'import sys;print("macos" if sys.platform=="darwin" else "linux")').json"
check "account-sync command installed"      '[ -x "$H/.local/bin/account-sync" ] && "$H/.local/bin/account-sync" --version | grep -q "1\."'
check "line endings protected in hub"      'grep -q "^\* -text" "$HUB/.gitattributes"'
check "hooks/statusLine kept per OS"       'grep -q statusLine "$OSF" && grep -q PostToolUse "$OSF" && ! grep -qE "statusLine|PostToolUse" "$HUB/settings.json"'
check "copied scripts are executable"      '[ -x "$H/.claude/hooks/fmt.sh" ] && [ -x "$H/.claude/statusline.sh" ]'

# A second machine running Windows shares the same hub (simulated: separate HOME + state, platform=windows).
W="$CSYNC_TEST_DIR/winhome"; mkdir -p "$W/.claude"
echo '{"statusLine":{"type":"command","command":"powershell -File C:/tools/status.ps1"},"permissions":{"allow":["Bash(dir *)"]}}' > "$W/.claude/settings.json"
( export HOME="$W" ACCOUNT_SYNC_PLATFORM=windows ACCOUNT_SYNC_HOSTNAME=pc
  python3 "$SCRIPT" init --hub "$HUB" >/dev/null && python3 "$SCRIPT" sync --quiet --skip-connectors )
check "PC got the Macs' shared settings"   'grep -q "pytest" "$W/.claude/settings.json"'
check "PC kept its own status line"        'grep -q "status.ps1" "$W/.claude/settings.json" && ! grep -q "statusline.sh" "$W/.claude/settings.json"'
check "PC status line stored for windows"  'grep -q "status.ps1" "$HUB/settings.windows.json"'
python3 "$SCRIPT" sync --quiet --skip-connectors
check "Mac/Linux got the PC permission"    'grep -q "dir \*" "$H/.claude/settings.json"'
check "Mac/Linux never got the PC script"  '! grep -rq "status.ps1" "$H/.claude" "$H/.claude-work" "$H/.claude-personal"'

echo "== Windows command rendering (simulated)"
check "Windows hook uses forward slashes"  'python3 -c "
import sys; sys.path.insert(0, \"$HERE/..\"); import account_sync as c
c.IS_WINDOWS = True; cmd = c.hook_command(); print(cmd)
assert \"\\\\\\\\\" not in cmd and cmd.endswith(\"sync --auto --quiet --background\")" >/dev/null'
check "Windows schedule uses schtasks"     'python3 -c "
import sys; sys.path.insert(0, \"$HERE/..\"); import account_sync as c
c.IS_WINDOWS = True; c.main([\"schedule\", \"--daily\", \"07:30\", \"--print\"])" | grep -q "/SC DAILY /ST 07:30"'

echo "== upgrade from a claude-sync setup (before the rename)"
L="$CSYNC_TEST_DIR/legacy"; mkdir -p "$L/.claude-sync/hub" "$L/.claude"
echo "{\"hub\": \"$L/.claude-sync/hub\", \"accounts\": []}" > "$L/.claude-sync/config.json"
echo '{"hooks":{"SessionStart":[{"matcher":"startup","hooks":[{"type":"command","command":"python3 ~/.claude-sync/claude_sync.py sync --auto"}]}]}}' > "$L/.claude/settings.json"
( export HOME="$L"; python3 "$SCRIPT" discover > "$L/discover.txt" 2>/dev/null; python3 "$SCRIPT" install-hook > /dev/null 2>&1 || true )
check "old setup moved to .account-sync"   'grep -q "$L/.account-sync/hub" "$L/.account-sync/config.json"'
check "old state folder not an account"    '! grep -q "\.claude-sync" "$L/discover.txt"'

echo "== settings page (account-sync ui)"
UIPORT=${CSYNC_UI_PORT:-28765}
python3 "$SCRIPT" ui --no-browser --port "$UIPORT" > "$CSYNC_TEST_DIR/ui.log" 2>&1 &
UIPID=$!; sleep 2
TOK=$(grep -o 't=[A-Za-z0-9_-]*' "$CSYNC_TEST_DIR/ui.log" | head -1 | cut -c3-)
U="http://127.0.0.1:$UIPORT"
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
# POST a JSON body (kept in a variable: bash 3.2 on macOS mangles escaped quotes inside $(...))
post() { curl -s -o /dev/null -w "%{http_code}" -H "X-Page-Token: $TOK" -H "Content-Type: application/json" --data-binary "$1" "$U$2"; }
check "page refuses a missing token"        '[ "$(code "$U/")" = 403 ] && [ "$(code "$U/?t=wrong")" = 403 ]'
check "page opens with the token"           '[ "$(code "$U/?t=$TOK")" = 200 ]'
check "API needs the token header"          '[ "$(code "$U/api/state")" = 403 ]'
check "API refuses other host names"        '[ "$(code -H "X-Page-Token: $TOK" -H "Host: attacker.example:$UIPORT" "$U/api/state")" = 403 ]'
check "API refuses non-JSON posts"          '[ "$(code -H "X-Page-Token: $TOK" -H "Content-Type: text/plain" --data-binary x "$U/api/config")" = 403 ]'
check "state lists every account"           'curl -s -H "X-Page-Token: $TOK" "$U/api/state" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if len(d[\"accounts\"])==3 and d[\"groups\"] else 1)"'
check "state never includes secret values"  '! curl -s -H "X-Page-Token: $TOK" "$U/api/state" | grep -qE "ghp_|hunter2|supersecret|sk-ant"'
B_PATH='{"sync_items":["../x"]}'; B_PROJ='{"sync_items":["projects"]}'; B_TIME='{"time":"25:00"}'
B_CASE='{"sync_items":["PROJECTS"]}'; B_NL='{"sync_items":["skills\n"]}'
check "config rejects path tricks"          '[ "$(post "$B_PATH" /api/config)" = 400 ]'
check "config refuses account data folders" '[ "$(post "$B_PROJ" /api/config)" = 400 ]'
check "schedule rejects a bad time"         '[ "$(post "$B_TIME" /api/run/schedule)" = 400 ]'
post '{"name":"CSYNC_TEST_KEY","value":"v123"}' /api/secret >/dev/null
check "saved key goes to the private file"  'grep -q v123 "$H/.account-sync/secrets.json" && [ "$(stat -c %a "$H/.account-sync/secrets.json" 2>/dev/null || stat -f %Lp "$H/.account-sync/secrets.json")" = 600 ]'
check "config refuses case variants"       '[ "$(post "$B_CASE" /api/config)" = 400 ]'
check "config refuses trailing newlines"    '[ "$(post "$B_NL" /api/config)" = 400 ]'
python3 - "$H/.claude-personal/settings.json" <<'PY'
import json, sys
p = sys.argv[1]; s = json.load(open(p)); s.setdefault("env", {})["GITHUB_TOKEN"] = "ghp_personalONLY987654"; json.dump(s, open(p, "w"))
PY
post '{"keys":["env","GITHUB_TOKEN"],"account":"personal"}' /api/resolve >/dev/null
python3 "$SCRIPT" sync --quiet --skip-connectors
check "Use everywhere keeps tokens out of the repo" '! grep -rq "ghp_personalONLY" "$H/.account-sync/hub" && grep -q ghp_personalONLY "$H/.claude-work/settings.json"'
post '{}' /api/quit >/dev/null; sleep 1
check "stop button stops the page"          '! kill -0 $UIPID 2>/dev/null'

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
