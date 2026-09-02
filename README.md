# Hermes Codex Limits

A unified [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that keeps OpenAI Codex subscription limits visible in the Hermes Desktop status bar.

![Status](https://img.shields.io/badge/Hermes-desktop%20plugin-7c3aed)
![License](https://img.shields.io/badge/license-MIT-green)

## What it shows

The status-bar chip displays the remaining 5-hour and weekly allowance:

```text
Codex 94% · 91%
```

Click it for:

- both allowance bars;
- exact reset times;
- ChatGPT plan name;
- banked reset-credit count;
- manual refresh;
- stale-data warning if OpenAI is temporarily unreachable.

## Token and network behavior

This plugin performs **no LLM inference**. The Desktop UI calls a small Python endpoint on the connected Hermes backend. That endpoint uses Hermes' existing `openai-codex` OAuth credentials and sends a read-only request to the Codex usage endpoint.

- no model tokens;
- no chat messages;
- no context growth;
- no prompt-cache invalidation;
- automatic checks share one five-minute cache across Desktop clients;
- manual refreshes are limited to one upstream attempt per 15 seconds;
- background polling pauses when the Desktop renderer is hidden.

## Requirements

- A recent Hermes Agent/Desktop build with unified plugin support.
- OpenAI Codex OAuth configured on the backend:

```bash
hermes auth add openai-codex
```

## Install

### One-click Desktop install

[Install in Hermes](hermes://plugin/install?repo=mkrb84/hermes-codex-limits&enable=1)

Hermes shows a confirmation screen and lets you select the backend and Desktop components. On additional computers connected to the same backend, install the Desktop component there as well.

### CLI backend install

```bash
hermes plugins install mkrb84/hermes-codex-limits --enable
hermes gateway restart
```

The Desktop half is local to each computer. Open the one-click link on every computer where you want the status indicator.

## Development

```bash
# Python backend tests
python -m unittest discover -s tests -p 'test_*.py' -v

# Desktop ESM contract tests
NODE_NO_WARNINGS=1 node --experimental-vm-modules --test tests/plugin.test.mjs

# Hermes package validation
hermes plugins doctor ./
```

## Privacy and security

OAuth tokens stay on the Hermes backend. The Desktop plugin receives only the plan, each window label, remaining percentage, reset time, banked reset count, and whether the snapshot is stale through Hermes' authenticated plugin API namespace. Secrets are never returned to the UI or written to this repository.

## License

MIT
