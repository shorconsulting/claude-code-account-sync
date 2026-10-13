# Account Sync privacy policy

Effective 2026-10-13. Publisher: David Shor (Shor Consulting).

Account Sync is a tool that runs entirely on your own computers. The publisher runs no server for it and receives no data from it: no telemetry, no analytics, no crash reports, no accounts.

## What Account Sync handles

- **Your Claude Code configuration**: settings, skills, agents, commands, rules, output styles, `CLAUDE.md`, user-level MCP servers, plugins and marketplaces, as listed in the README's "What it syncs". These can contain anything you wrote in them, including personal information.
- **Tokens and API keys inside that configuration**, such as an MCP server's `Authorization` header. They are replaced with placeholders before anything is shared, and the real values stay in `~/.account-sync/secrets.json` on your computer (mode 600, never committed).
- Sign-in files (`.credentials.json`, OAuth state in `.claude.json`) are never touched. Account-specific values such as `ANTHROPIC_API_KEY` stay in their own account.

## Where data goes

Only to places you choose and control:

- **The other Claude Code accounts on your computers.**
- **Its hub**, `~/.account-sync/hub`, a local git repo. It is pushed only to the private git remote you set with `init --remote`, if any. Your git host's privacy policy applies there.
- **The official `claude` CLI**, which Account Sync uses to add MCP servers, marketplaces and plugins. Installing a plugin downloads it from the source its marketplace names.

Nothing is sent to the publisher, to Anthropic (beyond what the `claude` CLI itself does), or to anyone else. When you use Account Sync through Claude Code, the commands' output is shown to Claude in your session as with any other command.

## Retention and deletion

Account Sync keeps nothing outside your own computers and git remote. To remove its data, delete `~/.account-sync/` on each computer and the private hub repository, if you made one. Backups of files it changed are in `~/.account-sync/backups/`.

## Children

Account Sync is a tool for developers and is not intended for anyone under 18.

## Changes and contact

Changes to this policy are published in this file, with its history in the repository. Questions: open an issue at https://github.com/shorconsulting/claude-code-account-sync/issues.
