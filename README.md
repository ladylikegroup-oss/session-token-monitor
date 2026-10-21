# session-token-monitor

OpenClaw skill for cumulative session-context monitoring, transcript-size footers, and source diagnostics.

## Recommended install

The installer makes the decisions for you: it finds the workspace, preserves
existing agent rules, backs up an older install, installs the skill and hook,
then verifies both before reporting success.

```bash
python3 install.py
```

If OpenClaw uses a non-default workspace:

```bash
python3 install.py --workspace /path/to/openclaw-workspace
```

Only treat the install as successful when the command prints both `INSTALL_OK`
and `VERIFY_OK`. Then start a new session or reload OpenClaw so the skill catalog
refreshes.

For AI agents, use the installer instead of copying files or editing `AGENTS.md`
manually. If installation fails, report the single `INSTALL_FAILED` line; do not
improvise a different install path.

Verify an existing installation without changing it:

```bash
python3 install.py --check
```

## Manual install fallback

If the automatic workspace detection cannot support a non-standard layout,
copy `skills/session-token-monitor` into the workspace `skills/` directory,
then run its hook installer:

```bash
cd /path/to/openclaw-workspace
python3 skills/session-token-monitor/scripts/install_agent_hook.py AGENTS.md
```

## Install from packaged skill

The packaged artifact is:

```text
dist/session-token-monitor.skill
```

A `.skill` file is a zip archive. If your OpenClaw setup does not provide a direct local `.skill` installer, unzip it and place the contained `session-token-monitor/` folder under your workspace `skills/` directory. Then run the hook installer above.

## What it does

- Adds mandatory reply footer rules: transcript size + cumulative Context high-water + current-session compaction count
- Keeps normal replies quiet at every context size while preserving data-integrity diagnostics
- Provides a dependency-free helper script: `scripts/session_footer.py` that requires an exact session selector by default and avoids single-call token totals
- Provides an idempotent hook installer: `scripts/install_agent_hook.py`
- Avoids storing or printing API keys, tokens, cookies, or recovery codes


## Diagnostics and anomaly warnings

The helper refuses to guess cumulative context from per-call usage fields. It parses a fresh runtime Context sample via `--status-text` / `--status-file`, then keeps the highest trusted sample for the current Session and compaction segment. Lower samples do not make the footer go backward. Reset/new Session or a changed compaction count starts a new segment. History and audit files are owner-only, and history updates use atomic replacement. Use `--audit-log <path>` for customer troubleshooting.

### OpenClaw canonical SQLite support

When `~/.openclaw/agents/main/agent/openclaw-agent.sqlite` exists, the helper
reads that authoritative store with an exact `--session-key`. Use
`--sessions-db /path/to/openclaw-agent.sqlite` for another agent. It never falls
back to stale migration JSON if the canonical database is unreadable or a key
is absent. An explicit `--sessions-json` retains legacy/offline support.

The footer always includes `🧹 對話壓縮：<count> 次`, including zero. A fresh
runtime count wins; otherwise it uses only the selected current generation's
`compactionCount`. Missing/null counts follow native status behavior (zero);
malformed counts remain `unknown`. Counts are not summed across old sessions.

SQLite reads use `mode=ro` and `PRAGMA query_only=ON`, project only session ID
and compaction count from metadata, and aggregate current-generation logical
UTF-8 event payload bytes. They never return dialogue, full metadata,
credentials, or persisted token totals. Cold/unsupported/incomplete transcript
byte measurements remain unknown without hiding a known compaction count.

## Agent hook

The installer adds this marker block to `AGENTS.md` or another always-loaded instruction file:

```md
<!-- session-token-monitor:start -->
Before every user-visible reply, use the `session-token-monitor` skill and append its transcript/cumulative-context/compaction footer. Treat this as a required pre-send gate; if footer collection fails, report the short failure instead of guessing.
<!-- session-token-monitor:end -->
```

The full monitoring logic remains in the skill. The hook is intentionally thin so updates stay centralized.

## Publish safety

Before publishing, scan for secrets. This repository should not contain API keys, OAuth tokens, cookies, recovery codes, `.env`, local config, transcripts, or customer memory files.

## Maintainer use of Codex

This project is maintained as part of the OpenClaw ecosystem. We plan to use Codex to review pull requests, improve compatibility with OpenClaw session/runtime changes, expand safety checks for transcript and token footers, and keep installation documentation current.

API-assisted maintenance should focus on issue triage, regression tests, documentation updates, and release notes. Codex should not be used to collect, store, or reveal private transcripts, API keys, OAuth tokens, cookies, or recovery codes.

## Token source rules

Use runtime/session status `📚 Context: <used>/<limit>` as the source for each observed sample. The footer displays the trusted high-water mark since the current Session or compaction segment began; it is not a sum of repeated API usage. Do not display message API `usage.totalTokens` as cumulative context. The compaction footer uses the fresh status value when supplied, otherwise the exactly selected session's `compactionCount`, matching OpenClaw's native status behavior.

For automation, pass parsed runtime values into the helper:

```bash
python3 skills/session-token-monitor/scripts/session_footer.py \
  --session-key 'agent:main:discord:channel:123' \
  --context-tokens 101000 \
  --context-limit 272000
```

Without `--session-key`, `--channel-id`, or `--to`, the helper fails closed unless `--allow-latest` is explicitly provided.

## Maintainer verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile skills/session-token-monitor/scripts/*.py scripts/*.py tests/*.py
python3 scripts/build_skill_archive.py --check
```

Rebuild the packaged skill after source changes with `python3 scripts/build_skill_archive.py`.

Before pushing, accumulate a reviewable change and run `bash scripts/check_push.sh`. Non-main branches run Branch Check; pull requests and `main` run full CI. An open PR suppresses duplicate branch tests, superseded runs are cancelled, and genuine failure notifications remain enabled.
