# nakama (仲間)

Coding agents work well alone and badly in groups. Hand one a big task and it
loses the thread; run three side by side and they trample each other's files.
nakama is a small way to run a *crew*: you split the work, each worker gets its
own git worktree and its own [herdr](https://herdr.dev) pane, and a different
model reviews what they build before anything merges.

It is deliberately thin: a few skills and one CLI. No daemon, no MCP server, no
framework to learn. Workers and orchestrator talk through plain files and
[herdr](https://herdr.dev)'s own prompt and wait commands, so you can watch
every agent in its pane and step in whenever you like.

## How a session goes

You tell your agent, inside herdr, "use nakama-orchestrate to …". The
orchestrator proposes a split and waits for your OK. For each task it launches
a worker in a fresh [worktrunk](https://worktrunk.dev) worktree and writes a
`task.md` describing the goal, the constraints and the exact commands that
prove it's done.

The worker does the job, commits locally, and writes a `result.md` with a
`Status:` line and its evidence. The orchestrator is waiting on that file. When
it appears, it runs the repo's checks itself, then launches a reviewer on a
different model. Findings go back to the builder as a `fix-N.md`, for at most
two rounds. Then the work is merged or reported, and each worker's pane is torn
down. Worktrees are kept until you remove them.

Nothing here assumes the workers are special. A worker is any coding agent CLI
that herdr can run, given a persona and a file to read.

### Talking mid-task

Sometimes the plan changes while a worker is busy, or a worker would rather ask
than guess. For that there's a mailbox: `nakama send <to> <kind> <file>` puts a
message in the recipient's inbox, and for a live worker it also nudges its pane,
so the pointer arrives at its next step. A worker's `ask` or `blocker` to the
orchestrator wakes the orchestrator's `nakama wait`. Workers may only message
the orchestrator unless their role opts in with `peers: true`.

## Which agents work today

The orchestration is agent-agnostic; the launch step isn't yet. nakama can
start **pi** and **Claude Code** workers out of the box, because those are the
two CLIs it has launch flags and herdr integrations for (`kind: pi|claude` in a
role). Supporting another CLI is a small, local change: teach `agent_args` in
`bin/nakama` how to start it, and make sure herdr can detect its state. Roles
choose the CLI and model per task, so mixing them is normal.

## Requirements

herdr ≥ 0.9.3 (with `herdr integration install` for each agent CLI you use),
worktrunk (`wt`), git ≥ 2.31, python3 ≥ 3.9, and at least one supported agent
CLI.

## Install

```bash
git clone https://github.com/<you>/nakama ~/.local/share/nakama
~/.local/share/nakama/install.sh     # symlinks + nakama doctor
```

Update with `git -C ~/.local/share/nakama pull`; uninstall with
`~/.local/share/nakama/install.sh --uninstall`. `install.sh` links
`~/.local/bin/nakama` and both skills into `~/.agents/skills` and
`~/.claude/skills`, and never overwrites a file it did not create. Any clone
location works.

Per repo (optional): `nakama init <gitignored files worktrees need>`, for
example `nakama init .env local.properties`. It writes `.worktreeinclude` and
hides it via `.git/info/exclude`, so nothing gets committed.

## Driving it yourself

The orchestrator skill is just a script of these commands:

```bash
nakama launch builder fix-login      # b1-fix-login: own tab, worktree on agent/b1-fix-login
$EDITOR <dir>/task.md                # format: skills/nakama/SKILL.md
nakama prompt b1-fix-login task.md
nakama wait b1-fix-login             # repeat while it says running
nakama teardown b1-fix-login
```

## Roles

A role is a markdown file: frontmatter plus a persona. Bundled ones live in
`roles/`; add or override yours in `~/.config/nakama/roles/`. `nakama roles`
lists them all. Frontmatter fields:

| field | meaning |
|---|---|
| `prefix` | name prefix for launched agents (`b` gives `b1-…`) |
| `kind` | agent CLI to run: `pi` or `claude` |
| `model` | model id for that CLI (optional) |
| `mode` | `read` or `write` |
| `budget` | minutes before `nakama wait` reports `over_budget` |
| `names` | optional name pool |
| `spawns` | optional; lets the role launch its own agents (off by default, so a worker can't recursively spawn a crew) |
| `peers` | optional; lets the role message peers, not only the orchestrator (off by default) |

Claude workers always get an explicit `--permission-mode` (`acceptEdits` for
write, `default` for read), because a user default of auto mode would override
the allow list. Allow your project's build and test commands in that repo's
`.claude/settings.local.json`; Claude reads it from worktrees too.

## Safety

Worker panes get environment variables that make `git push`, git over HTTPS and
`gh` fail. That is an accident guard, not a sandbox: an agent that deliberately
clears its environment can get around it.

Read-only roles are enforced only for Claude workers (permission mode
`default` with a read allow list). For pi workers they are an instruction
contract, because pi's read tool list still includes `bash` and `write`. The
orchestrator checks afterwards that `git status`, `HEAD` and `git stash list`
are unchanged.

## Development

`python3 tests/test_nakama.py` runs against fake `herdr` and `wt` binaries, so
no herdr session is needed.
