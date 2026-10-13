---
name: sync
description: Keep the user's Claude Code accounts identical, across several accounts on one computer and across computers (macOS, Windows, Linux). Use when the user wants to sync, copy or share Claude Code settings, skills, agents, commands, MCP servers or plugins between accounts or machines, set up a new account with everything the others have, or see what an account is missing.
---

# Account Sync

Account Sync merges everything from all of the user's Claude Code accounts (settings.json, skills, agents, commands, rules, output styles, CLAUDE.md, user-scope MCP servers, plugins and marketplaces) into one shared set, then installs that set into every account. With a private git repository it does the same across computers.

## How to run it

- The tool is `${CLAUDE_PLUGIN_ROOT}/account_sync.py`, a standard-library Python 3.8+ script.
- Start Python with `python3` on macOS and Linux and with `py -3` on Windows.
- After the first `init`, prefer the installed `account-sync` command. If that command isn't found yet, run `python3 ~/.account-sync/account_sync.py` instead, or on Windows `py -3 "%USERPROFILE%\.account-sync\account_sync.py"`.
- Every command except `init` needs `init` to have run once on this computer. Check whether `~/.account-sync/config.json` exists.

## Workflow

1. **Not set up yet:** run `python3 "${CLAUDE_PLUGIN_ROOT}/account_sync.py" init`. It finds every Claude Code account, creates `~/.account-sync`, and adds the `account-sync` command. It changes no account. To share between computers, ask the user for an empty **private** git repository URL and use `init --remote <url>`.
2. **Always preview first:** run `account-sync status`, a dry run that changes nothing. Summarise for each account what it would gain: files, settings, MCP servers, plugins. Point out the "settings conflict" and "differs between accounts" lines.
3. **Ask before syncing,** then run `account-sync sync`. Report the summary and where the backups went.
4. **Automation, only when the user wants it:** `account-sync schedule` (daily at 09:00, or `--daily HH:MM`) and `account-sync install-hook` (also syncs when Claude Code starts).
5. **A visual view:** if the user would rather click than type, suggest they run `account-sync ui` in their own terminal. It opens a settings page in their browser and keeps running in that window. Don't start it yourself: it waits until they close it.

## Rules

- Get the user's explicit OK before `apply --prune` (it removes items that aren't in the hub), `apply --force` (it overwrites MCP servers and newer local files) or `--prefer <account>`.
- Never print or copy `~/.account-sync/secrets.json`. It holds the real values of tokens that were stripped from the hub.
- claude.ai connectors, and skills or plugins synced from claude.ai, live in each claude.ai account and can't be written locally. Relay the list of what each account is missing from the output, and tell the user to add them at https://claude.ai/customize/connectors while signed in to that account.
- To delete something everywhere, the user removes it from the hub (`~/.account-sync/hub`) and runs `apply --prune`. A normal sync would bring it back, because sync is a union.
