# Security

## Reporting a vulnerability

Please report security problems privately through GitHub: **Security → Report a vulnerability** on this repository. For anything else, open an issue.

## What Account Sync does with credentials

Account Sync syncs your own Claude Code configuration between your own accounts. That configuration can hold tokens, such as an MCP server's `Authorization` header or a `GITHUB_TOKEN` in settings `env`. Account Sync reads `~/.claude.json` and each account's `settings.json` to sync user-level MCP servers and settings. While doing so it:

- swaps every token for a `${CSYNC_…}` placeholder before anything is written to the shared hub;
- keeps the real values in `~/.account-sync/secrets.json` on your computer (mode 600, never committed);
- writes them back only into your own accounts' config on that computer;
- never touches sign-in files (`.credentials.json`, OAuth state in `.claude.json`), and never copies account-specific values such as `ANTHROPIC_API_KEY` to another account or to the hub.

No credential is sent to any host. Data leaves your computer only through the private git remote you set with `init --remote` (the hub, with secrets already removed) and the official `claude` CLI. See [PRIVACY.md](PRIVACY.md).

## Automated scanner findings, explained

Automated plugin scanners pair any URL in a file with any credential the plugin reads, and flag shell pipes. These findings are false positives:

| Where | What is flagged | What it actually is |
|---|---|---|
| `README.md` | `raw.githubusercontent.com`, `curl` | An optional line that downloads `account_sync.py` from this repository. It sends nothing. |
| `account_sync.py` | `claude.ai` | A printed link to claude.ai/customize/connectors, where you add connectors by hand. Nothing is sent there. |
| `account_sync.py` | `127.0.0.1` | The settings page, which listens only on this computer, behind a random per-run token. |
| `tests/fake_claude`, `tests/make_fake_home.sh`, `tests/windows_check.py` | `example.com`, `api.githubcopilot.com`, `ghp_…`, `sk-ant-…` | Fake accounts and MCP servers with made-up tokens. The tests check that these tokens never reach the hub. |
| `tests/*` | `curl …` piped into `grep` or `python3` | Requests to the local settings page at 127.0.0.1, with the answer checked locally. Tests run only when a developer starts them by hand. |

The plugin fetches and runs nothing from the network at install or run time. Plugins it installs for you come from the sources their own marketplaces name, through `claude plugin install`.
