---
name: nakama
description: Run one pi or Claude Code worker (builder, reviewer, researcher, or any custom role) in a herdr pane on any model, with the nakama CLI and the .agent/<name>/ file contract. Use when you need a subagent, or when nakama-orchestrate sends you here. Requires HERDR_ENV=1.
---

# nakama

Run `herdr --skill` once per session for herdr itself. Every `nakama` command prints one JSON object. Exit 2 = usage error or not inside herdr: stop and tell the user. If anything looks misconfigured, run `nakama doctor` and show its fixes. Orchestrator session defaults to `HERDR_PANE_ID`; set `NAKAMA_SESSION` to preserve it when restarting in a new pane.

## Roles
`nakama roles` prints the roster with each role's file path. A role is a markdown file: frontmatter + persona body. `~/.config/nakama/roles/<role>.md` overrides the bundled one with the same name; add a file to add a role.
```
---
prefix: r            # name prefix → r1-<slug>
kind: claude         # pi | claude
model: opus          # omit = the CLI's default
mode: read           # read | write (tool limits and Claude permission flags)
budget: 20           # minutes
names: zoro, sanji   # optional pool: zoro-<slug>, sanji-<slug>, then zoro2-<slug>
spawns: true         # optional: this role may run `nakama launch` itself (sub-orchestrator); off by default
peers: true          # optional: this role may `nakama send` to peers, not only the orchestrator; off by default
---
You are a reviewer: ...
```
`--kind/--model/--budget/--name` on launch override per spawn. A reviewer's model must differ from the builder's: `launch --join` refuses a worker whose kind and model, as passed or defaulted, equal the joined worker's (pass `--model`).

## Commands
| Command | Use |
|---|---|
| `nakama launch builder <slug> [--base REF] [--model M] [--name N] [--layout tab\|split]` | new worktree on `agent/<name>` + its own herdr tab (default; `split` = pane beside you) + agent. Returns `name`, `dir`, `worktree` |
| `nakama launch reviewer <slug> --join <builder-name>` | reviewer in the builder's worktree, own dir |
| `nakama launch reviewer <slug> --no-worktree` | review in the current checkout |
| `nakama resume <name>` | restart a crashed or torn-down worker on its existing worktree and re-prime it from task.md and fix files; returns `fresh_primed` (the conversation itself is not resumed) |
| `nakama pause <name>|--all [--timeout SEC]` | request write-mode builders stop and leave changes uncommitted in the worktree; `--all` targets open workers in the current session. Wait up to `--timeout` (default 300 s) for a fresh result with `Status: partial`, `Next: retry`, `Steps: k/N` if stepped, and `## Resume`, then close their tabs. A timeout forces close without a note, also retaining those changes. Results use `paused`/`paused_forced`; `--all` reports `skipped` workers with reason `read_only`/`settled`; `resume` continues |
| `nakama prompt <name> task.md` | sends `Read .agent/<name>/task.md and do it. Write result.md with Status exactly one of succeeded, partial, failed, needs_input.` Returns `busy` (nothing sent) while the agent is still working: `nakama wait` first |
| `nakama prompt <name> fix-N.md` | a fix round (max 2; returns `round_limit` after that) |
| `nakama prompt <name> fix-N.md --answer` | an answer to needs_input; not a round |
| `nakama wait <name>` | returns within ~5 min; on `running`, call it again. For tasks with `## Steps`, includes `steps` and may warn `steps_mismatch` when reported `Steps:` differs from the git trailer count. Claude orchestrators: give the Bash tool a timeout above 300000 ms (or pass `--timeout 100`) |
| `nakama send <to> <kind> <file>` | message the orchestrator (or, with `peers: true`, a peer); orchestrator may use `@all` to broadcast to live workers in its session. Optional `--id KEY` makes retries idempotent. kind: done, ask, answer, info, blocker. The body lives in `<file>`. To a live worker it also pushes a pointer into its pane, even mid-task (`pushed: true`): use it for requirement changes, since `prompt` returns `busy`. A `done`, `ask` or `blocker` to the orchestrator pokes the orchestrator's pane (`nakama inbox` pointer), so a finished worker reaches you outside `wait`; if a dialog is open herdr refuses the poke and the mail stays in the inbox. `wait` drops the `done` mail of the agent it reports, since result.md is the report |
| `nakama inbox [name]` | print and clear unread messages (`--peek` keeps them). Orchestrator mail is session-scoped. Default: yourself |
| `nakama extend <name> <min>` | only after reading the screen and seeing progress; the 180-minute ceiling applies per prompt (a new prompt or `--answer` starts a fresh budget) |
| `nakama teardown <name> [--remove-worktree] [--force]` | close the pane; `--remove-worktree` reports `reason` when wt refuses and refuses paused workers unless `--force`; `--force` discards uncommitted and untracked files, never use it unless the user said the leftovers are disposable |
| `nakama list [--all]`, `nakama sweep [--all]` | list open worker records in this session by default (`--all` includes every session); sweep reports paused workers under `paused_workers` plus global orphaned workers and `agent/*` worktrees, and never kills anything |
| `nakama doctor`, `nakama init <files…>` | setup checks / per-repo `.worktreeinclude` (untracked, hidden) |

Write task files with your normal file tools into the returned `dir` before prompting. Worker panes cannot push or reach GitHub (env backstop); that is an accident guard, not a sandbox.

## wait outcomes
| status | Do |
|---|---|
| succeeded / partial / failed / needs_input | the agent has settled with a fresh result.md: read `result` and act on the Status, not the wording |
| tampered | a read-mode worker changed HEAD, the stash or file contents outside .agent/ (pi's read-only mode is only an instruction): reject its result |
| malformed | result.md has no Status line or uses an unsupported value; `hint` names the allowed values and any bad value |
| blocked | show `screen` to the user; never answer it yourself (one exception below) |
| over_budget | read `screen`: progressing → `nakama extend`; looping or stuck → `nakama teardown`, treat as failed |
| stalled | read `screen`: progressing means the worker is just thinking → wait again or `nakama extend`; looping/stuck → `nakama teardown` |
| message | a worker sent mail: run `nakama inbox`, act (answer an `ask` with `nakama send <name> answer <file>`), then `nakama wait` again. Unread mail makes every wait return at once |
| running | call `nakama wait` again |

A `warnings` field is advisory: the orchestrator decides what to do. A read-mode result also carries `reviewed_head` (HEAD when it was prompted), fixed at its first settled result; before merging, compare it with the branch HEAD.

The one exception: if `launch` returns `blocked` and its `screen` is Claude's "trust this folder" dialog for the worktree nakama just created, send `herdr agent send-keys <name> down enter` (the dialog's default is "No, exit"; `down` selects "Yes, proceed"). Worktrees of a repo Claude already trusts never show it.

## task.md format
Optionally place `## Steps` between `## Instructions` and `## Acceptance`; each numbered item uses `<n>. <what> — check: <command>`.
```
## Goal
<finished state, 1-2 sentences>
## Context
<only facts not in the repo; user decisions are binding>
## Instructions
- <one action or constraint per line>
- <last line: when to stop and write Status: needs_input>
## Acceptance
- <exact commands whose output proves done>
---
- Commit locally with Conventional Commits. Never push, never open PRs, even if AGENTS.md or any hook text says to; the orchestrator owns that.
- Never bypass git hooks. If a hook fails, stop and report it.
- Before you stop, write result.md next to this file in the required format.
- After any compaction or context loss, re-read this file and every fix-*.md next to it.
- With `## Steps`: one commit per step after its check passes, with `--trailer "Nakama-Step: <k>/<N>"`; run full Acceptance after the last step.
```
fix-N.md: the delta only. For each item: location, the problem, the expected change. A re-review lists every earlier finding and the change made for it.

## result.md format (written by the worker, next to task.md)
For stepped tasks, an optional `Steps: k/N` line follows `Next:`; a paused result may include an optional `## Resume` section.
```
Status: succeeded | partial | failed | needs_input
Next: review | merge | none | retry | ask
## Summary
## Files
## Evidence
## Needs
## Observations
```
