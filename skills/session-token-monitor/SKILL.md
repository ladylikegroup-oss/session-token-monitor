---
name: session-token-monitor
description: "Mandatory OpenClaw reply-footer workflow for every user-visible response: monitor cumulative session context and transcript size, append consistent token/transcript footers, preserve source diagnostics, and report short-term/weekly usage when asked about session, token, context, compaction, reset, transcript size, usage quota, or LLM conversation health."
---

# Session Token Monitor

## Purpose

Use this skill to keep OpenClaw conversations observable as they grow. It standardizes cumulative session-context footers, transcript-size classification, source diagnostics, and concise status answers without routine capacity warnings.

This skill is intentionally simple and model-agnostic: any LLM can follow it.

## Mandatory Reply Footer

For every user-visible reply, collect session status and append a footer. Treat this as a pre-send gate, not an optional status command.

Preferred sources, in order:

1. Use the runtime/session status tool for a fresh context sample (`📚 Context: <used>/<limit>`) and current-session compaction count.
2. Run `scripts/session_footer.py` from this skill for transcript size, passing an exact `--session-key`, `--channel-id`, or `--to`.
3. If neither works, state that the footer is unavailable; do not guess.

**Always re-fetch fresh on every reply.** Do not increment by feel (e.g. "+2K every turn"). The runtime status supplies each observed sample; the helper keeps the trusted high-water mark for the current session and compaction segment.

**Model-switch invalidates the limit.** Different models have different context windows (e.g. Codex GPT-5.5 ≈ 272K, Claude Opus 4.7 ≈ 1M, Claude Sonnet 4.6 ≈ 200K). When the active model changes mid-conversation, the previous limit is stale. Re-fetch from runtime status before reporting the next footer.

Footer format:

```text
📝 Transcript: <size> MB｜<level>
📊 累積 Context：<high-water> / <limit>
🧹 對話壓縮：<count> 次
```

`累積 Context` means the highest trusted runtime Context sample observed since this Session started or since its latest OpenClaw compaction. Lower samples do not make the displayed value go backward. A reset/new Session or a changed compaction count starts a new accumulation segment from the next trusted sample. It is not the sum of every API call and does not double-count repeated prompt tokens.

If that cumulative value exceeds the reported limit, render `📊 累積 Context：<high-water>｜上限待核對`. Preserve the raw limit in JSON diagnostics, but do not present a contradictory ratio as verified capacity or guess a replacement limit.

Do **not** use per-call API usage fields, message `usage.totalTokens`, or ambiguous `sessions.json.totalTokens` as the cumulative context count. If the runtime status is unavailable, show `📊 累積 Context：unknown / <limit>` or `unknown tokens` instead of guessing.

## Install-Time Agent Hook

OpenClaw skills teach the model what to do, but a skill alone is not a global middleware. For reliable every-reply behavior, installation should add this minimal hook to the target agent instructions (`AGENTS.md`, equivalent agent profile, or another always-loaded instruction file):

```md
<!-- session-token-monitor:start -->
Before every user-visible reply, use the `session-token-monitor` skill and append its transcript/cumulative-context/compaction footer. Treat this as a required pre-send gate: fetch fresh runtime/session status every time, pass the current `📚 Context: used/limit` sample into the footer helper, let the helper preserve the trusted high-water mark for the current session and compaction segment, and use the exactly selected session's compaction count. Never sum per-call usage or use message `usage.totalTokens` as cumulative context. If footer collection fails, report the short failure instead of guessing.
<!-- session-token-monitor:end -->
```

Use the bundled helper to add or update the marker block safely:

```bash
python3 skills/session-token-monitor/scripts/install_agent_hook.py AGENTS.md
```

The helper is idempotent: re-running it replaces only the marker block above. For uninstall, remove the marker block and the skill folder.

## Transcript Size Levels

- `< 1 MB` → `輕微`
- `1–3 MB` → `中度`
- `3–6 MB` → `擁擠`
- `> 6 MB` → `極限`

## Routine Capacity Alerts

Do not add routine 100K／130K／150K／200K capacity warnings above the footer. Native compaction and handoff own continuity; normal replies keep the three informational footer lines at every context size. Preserve anomaly diagnostics when the token source, session selection, context limit, or token movement is not trustworthy.

## When the User Asks About Session / Token / Quota

Answer with current model/runtime if available, cumulative context high-water and context limit, transcript size and level, current-session compaction count, 5-hour usage remaining/reset countdown, and weekly usage remaining/reset countdown. Discuss reset or summary only when the user explicitly asks.

Keep the answer concise. Do not expose private paths unless useful for debugging.

## Script Usage

Use `scripts/session_footer.py` to generate the transcript/context footer. Important options:

- `--json` for machine-readable output
- `--session-key <openclaw-session-key>` to select an exact session
- `--channel-id <id>` to select a channel session
- `--to channel:<id>` to select by OpenClaw recipient
- `--context-tokens <n>` to inject current context tokens parsed from runtime/session status
- `--context-limit <n>` to inject the context window limit parsed from runtime/session status
- `--status-text <text>` or `--status-file <path>` to parse `📚 Context: used/limit`, model, runtime, and compaction count from a status card
- `--token-source <label>` to explicitly label injected values; prefer `runtime-status`
- `--history-json <path>` to preserve the cumulative high-water mark and detect session／compaction boundaries
- `--audit-log <path>` to append JSONL diagnostics for customer troubleshooting
- `--no-history` to disable accumulation and report only `本輪 Context`
- `--allow-latest` to opt in to fallback by most recent session; avoid this for live reply footers
- `--sessions-json /path/to/sessions.json` to override the default session index
- `--sessions-db /path/to/openclaw-agent.sqlite` for an explicit read-only canonical store (mutually exclusive with `--sessions-json`)

When `~/.openclaw/agents/main/agent/openclaw-agent.sqlite` exists, it is authoritative and requires an exact `--session-key`; channel/recipient/latest selectors never scan the database. The helper reads only the current generation session ID, compaction count, and logical transcript-byte aggregates, never persisted Context/usage totals. Connections are read-only and no migrations run. A broken store or absent key never falls back to stale migration JSON. Explicit `--sessions-json` keeps legacy/offline selection available; without a canonical database the default is `~/.openclaw/agents/main/sessions/sessions.json`.

Always keep the compaction footer, including `0 次`. Fresh runtime status wins; otherwise use the exact current generation's `compactionCount`. Missing/null counts follow native status behavior (zero), but malformed values remain `unknown`, not a fabricated zero. Do not sum old generations. Unavailable transcript-byte measurement does not hide a known count.

The helper keeps a small local history at `~/.openclaw/session-token-monitor/history.json` by default. It stores the latest observed sample and the cumulative high-water mark for each exact Session. Ordinary lower runtime samples do not reduce the displayed cumulative value. A new Session ID or a changed OpenClaw compaction count resets the segment to the new trusted sample. The displayed compaction count comes from fresh runtime status when supplied, otherwise from the exactly selected session entry. Only trusted runtime-status or explicit CLI samples update history. History updates are atomic and owner-only (`0600`). Use `--no-history` only when you intentionally want the current sample instead of cumulative tracking.

Example when runtime status says `📚 Context: 101k/272k`:

```bash
python3 skills/session-token-monitor/scripts/session_footer.py \
  --session-key 'agent:main:discord:channel:123' \
  --context-tokens 101000 \
  --context-limit 272000
```

## Diagnostic Warnings

The helper emits inline warnings before the footer when it detects suspicious conditions:

- `Context token unavailable` — no reliable runtime context was provided, so it refuses to guess.
- `Context token source suspicious` — values came from a weak source instead of runtime status.
- `Context limit changed` — the same session's limit changed, often due to model switch or runtime reporting changes.
- `Context source conflict` — runtime status and explicit CLI values disagree; runtime status wins.

For customer debugging, run with `--audit-log logs/session-token-monitor.jsonl` so each footer writes a compact JSON record with time, session key, model, runtime, context tokens, context limit, source, compaction count, and warnings.

## Fallback Rules

If the fresh context sample is missing but transcript size is known, still show the transcript line, show `📊 累積 Context：unknown / <limit>` when a limit is known (or `unknown tokens`), and retain the selected session's compaction line. Do not silently reuse stale history as if it were fresh.

If transcript size is missing but runtime status has context tokens, omit the transcript line and show the context line.

If everything fails, output:

```text
[session footer unavailable: <short reason>]
```

## Safety Rules

- Never store API keys, OAuth tokens, cookies, recovery codes, or private credentials in this skill.
- Never print secrets while reporting session status.
- Before publishing this skill, scan the package for common secret patterns.
- Do not include local customer-specific memory, transcripts, or config files.
