#!/usr/bin/env bash
# Build a fake HOME with three Claude Code accounts: a brand-new default one and two populated ones.
set -e
T="${CSYNC_TEST_DIR:?set CSYNC_TEST_DIR}"
rm -rf "$T/home"; mkdir -p "$T/home"; H="$T/home"
mkdir -p "$T/fakebin"; cp "$(dirname "$0")/fake_claude" "$T/fakebin/claude"; chmod +x "$T/fakebin/claude"

# 1) Brand-new default install: logged in, nothing else.
mkdir -p "$H/.claude"
echo '{"claudeAiOauth":{"accessToken":"SECRET-OAUTH"}}' > "$H/.claude/.credentials.json"
echo '{"oauthAccount":{"emailAddress":"new@example.com"},"numStartups":1}' > "$H/.claude.json"
echo '{"marketplaces":{"claude-plugins-official":{"source":"github","repo":"anthropics/claude-plugins-official"}},"plugins":{},"connectors":["claude.ai Gmail"],"synced":[]}' > "$H/.claude/fake_state.json"

# 2) Work account
W="$H/.claude-work"; mkdir -p "$W/skills/review" "$W/skills/synced/pdf" "$W/agents" "$W/hooks"
cat > "$W/settings.json" <<'EOF'
{
  "permissions": {"allow": ["Bash(npm run *)", "Bash(git diff *)"], "deny": ["Read(./.env)"]},
  "env": {"GITHUB_TOKEN": "ghp_workTOKEN1234567890", "ANTHROPIC_API_KEY": "sk-ant-WORKONLY", "NODE_ENV": "development"},
  "hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "~/.claude-work/hooks/fmt.sh"}]}]},
  "apiKeyHelper": "/usr/local/bin/work-key",
  "theme": "dark",
  "enabledPlugins": {"formatter@team-market": true},
  "extraKnownMarketplaces": {"team-market": {"source": {"source": "github", "repo": "acme/team-market"}}}
}
EOF
printf -- '---\ndescription: Review code (WORK version, newer)\n---\nReview carefully.\n' > "$W/skills/review/SKILL.md"
printf 'checklist\n' > "$W/skills/review/checklist.md"
printf -- '---\nname: pdf\n---\nsynced from claude.ai\n' > "$W/skills/synced/pdf/SKILL.md"
printf -- '---\nname: reviewer\ndescription: reviews\n---\nYou review.\n' > "$W/agents/reviewer.md"
printf '# Work rules\n- Use conventional commits\n' > "$W/CLAUDE.md"
printf '#!/bin/sh\necho fmt\n' > "$W/hooks/fmt.sh"
cat > "$W/.claude.json" <<'EOF'
{"oauthAccount":{"emailAddress":"work@example.com"},
 "mcpServers": {
   "github": {"type":"http","url":"https://api.githubcopilot.com/mcp/","headers":{"Authorization":"Bearer ghp_mcpSECRETvalue123456"}},
   "sentry": {"type":"http","url":"https://mcp.sentry.dev/mcp"}
 },
 "projects": {"/repo": {"mcpServers": {"local-only": {"command":"x"}}}}}
EOF
echo '{"marketplaces":{"claude-plugins-official":{"source":"github","repo":"anthropics/claude-plugins-official"},"team-market":{"source":"github","repo":"acme/team-market"}},"plugins":{"formatter@team-market":true,"skill-creator@claude-plugins-official":true},"connectors":["claude.ai Slack","claude.ai Linear"],"synced":["deck@synced"]}' > "$W/fake_state.json"

# 3) Personal account (older review skill, different settings)
P="$H/.claude-personal"; mkdir -p "$P/skills/review" "$P/skills/notes" "$P/commands"
cat > "$P/settings.json" <<'EOF'
{
  "permissions": {"allow": ["Bash(npm run *)", "Bash(pytest *)"]},
  "model": "opus",
  "theme": "light",
  "statusLine": {"type": "command", "command": "~/.claude-personal/statusline.sh"}
}
EOF
printf -- '---\ndescription: Review code (PERSONAL version, older)\n---\nReview.\n' > "$P/skills/review/SKILL.md"
touch -t 202601010000 "$P/skills/review/SKILL.md"
printf -- '---\ndescription: Take notes\n---\nNotes.\n' > "$P/skills/notes/SKILL.md"
printf 'Fix issue $ARGUMENTS\n' > "$P/commands/fix.md"
printf '#!/bin/sh\necho status\n' > "$P/statusline.sh"
cat > "$P/.claude.json" <<'EOF'
{"oauthAccount":{"emailAddress":"me@example.com"},
 "mcpServers": {"db": {"type":"stdio","command":"npx","args":["-y","@bytebase/dbhub","--api-key","supersecretdbkey"],"env":{"DB_PASSWORD":"hunter2"}}}}
EOF
echo '{"marketplaces":{"claude-plugins-official":{"source":"github","repo":"anthropics/claude-plugins-official"}},"plugins":{"mcp-server-dev@claude-plugins-official":false},"connectors":["claude.ai Gmail","claude.ai Slack"],"synced":[]}' > "$P/fake_state.json"
# Shell profile that points at the personal dir (discovery should parse this)
echo 'alias claude-me="CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude"' > "$H/.zshrc"
echo "fake home ready at $H"
