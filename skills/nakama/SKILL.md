---
name: nakama
description: Run one pi or Claude Code worker (builder, reviewer, researcher, or any custom role) in a herdr pane on any model, with the nakama CLI and the .agent/<name>/ file contract. Use when you need a subagent, or when nakama-orchestrate sends you here. Requires HERDR_ENV=1.
---

# nakama

Run `herdr --skill` once per session for herdr itself. Every `nakama` command prints one JSON object. Exit 2 = usage error or not inside herdr: stop and tell the user. If anything looks misconfigured, run `nakama doctor` and show its fixes.

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
`--kind/--model/--budget/--name` on launch override per spawn. A reviewer's model must differ from the builder's.

## Commands
| Command | Use |
|---|---|
| `nakama launch builder <slug> [--base REF] [--model M] [--name N] [--layout tab\|split]` | new worktree on `agent/<name>` + its own herdr tab (default; `split` = pane beside you) + agent. Returns `name`, `dir`, `worktree` |
| `nakama launch reviewer <slug> --join <builder-name>` | reviewer in the builder's worktree, own dir |
| `nakama launch reviewer <slug> --no-worktree` | review in the current checkout |
| `nakama resume <name>` | restart a crashed or torn-down worker on its existing worktree and re-prime it from task.md and fix files; returns `fresh_primed` (the conversation itself is not resumed) |
| `nakama prompt <name> task.md` | sends `Read .agent/<name>/task.md and do it.` Returns `busy` (nothing sent) while the agent is still working: `nakama wait` first |
| `nakama prompt <name> fix-N.md` | a fix round (max 2; returns `round_limit` after that) |
| `nakama prompt <name> fix-N.md --answer` | an answer to needs_input; not a round |
| `nakama wait <name>` | returns within ~5 min; on `running`, call it again. Claude orchestrators: give the Bash tool a timeout above 300000 ms (or pass `--timeout 100`) |
| `nakama send <to> <kind> <file>` | message the orchestrator (or, with `peers: true`, a peer); orchestrator may use `@all` to broadcast to live workers. Optional `--id KEY` makes retries idempotent. kind: done, ask, answer, info, blocker. The body lives in `<file>`. To a live worker it also pushes a pointer into its pane, even mid-task (`pushed: true`): use it for requirement changes, since `prompt` returns `busy` |
| `nakama inbox [name]` | print and clear unread messages (`--peek` keeps them). Default: yourself |
| `nakama extend <name> <min>` | only after reading the screen and seeing progress; the 180-minute ceiling applies per prompt (a new prompt or `--answer` starts a fresh budget) |
| `nakama teardown <name> [--remove-worktree] [--force]` | close the pane; `--remove-worktree` reports `reason` when wt refuses; `--force` discards uncommitted and untracked files, never use it unless the user said the leftovers are disposable |
| `nakama list`, `nakama sweep` | open agents / orphans. sweep never kills; ask the user first |
| `nakama doctor`, `nakama init <files…>` | setup checks / per-repo `.worktreeinclude` (untracked, hidden) |

Write task files with your normal file tools into the returned `dir` before prompting. Worker panes cannot push or reach GitHub (env backstop); that is an accident guard, not a sandbox.

## wait outcomes
| status | Do |
|---|---|
| succeeded / partial / failed / needs_input | the agent has settled with a fresh result.md: read `result` and act on the Status, not the wording |
| malformed | result.md has no Status line: treat as failed, or send one fix asking for the format |
| blocked | show `screen` to the user; never answer it yourself (one exception below) |
| over_budget | read `screen`: progressing → `nakama extend`; looping or stuck → `nakama teardown`, treat as failed |
| stalled | read `screen`: progressing means the worker is just thinking → wait again or `nakama extend`; looping/stuck → `nakama teardown` |
| message | a worker sent mail: run `nakama inbox`, act (answer an `ask` with `nakama send <name> answer <file>`), then `nakama wait` again. Unread mail makes every wait return at once |
| running | call `nakama wait` again |

A `warnings` field is advisory: the orchestrator decides what to do.

The one exception: if `launch` returns `blocked` and its `screen` is Claude's "trust this folder" dialog for the worktree nakama just created, send `herdr agent send-keys <name> down enter` (the dialog's default is "No, exit"; `down` selects "Yes, proceed"). Worktrees of a repo Claude already trusts never show it.

## task.md format
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
```
fix-N.md: the delta only. For each item: location, the problem, the expected change. A re-review lists every earlier finding and the change made for it.

## result.md format (written by the worker, next to task.md)
```
Status: succeeded | partial | failed | needs_input
Next: review | merge | none | retry | ask
## Summary
## Files
## Evidence
## Needs
## Observations
```
