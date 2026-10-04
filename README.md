# nakama (仲間)

A crew of coding agents in [herdr](https://herdr.dev) panes. An orchestrator (Claude Code or pi) splits work, runs pi / Claude Code builders in [worktrunk](https://worktrunk.dev) worktrees, gates them, has a different model review them, and loops up to two fix rounds. Plain skills plus one small CLI: no daemon, no MCP server.

## Requirements
herdr ≥ 0.9.3 (with `herdr integration install pi|claude`), worktrunk (`wt`), git ≥ 2.31, python3 ≥ 3.9, and pi and/or Claude Code.

## Install
```bash
git clone https://github.com/<you>/nakama ~/.local/share/nakama
~/.local/share/nakama/install.sh     # symlinks + nakama doctor
```
Update: `git -C ~/.local/share/nakama pull`. Uninstall: `~/.local/share/nakama/install.sh --uninstall`.
`install.sh` links `~/.local/bin/nakama` and both skills into `~/.agents/skills` (pi and other agents) and `~/.claude/skills`. It never overwrites a file it did not create. Any clone location works.

Per repo (optional): `nakama init <gitignored files worktrees need>`, e.g. `nakama init .env local.properties`. It writes `.worktreeinclude` and hides it via `.git/info/exclude`, so nothing gets committed.

## Use
Inside herdr, ask your agent: "use nakama-orchestrate to …". Or drive it yourself:
```bash
nakama launch builder fix-login            # → b1-fix-login in its own tab, worktree on agent/b1-fix-login
$EDITOR <dir>/task.md                      # format: skills/nakama/SKILL.md
nakama prompt b1-fix-login task.md
nakama wait b1-fix-login
nakama teardown b1-fix-login
```

## Roles
`roles/*.md`, overridable and extendable in `~/.config/nakama/roles/`. Frontmatter: `prefix`, `kind` (pi|claude), `model`, `mode` (read|write), `budget` (minutes), `names` (optional name pool); the body is the persona. `nakama roles` lists them.

Claude workers always get an explicit `--permission-mode` (`acceptEdits` for write, `default` for read), because a user default of auto mode would override the allow list. Allow your project's build and test commands in that repo's `.claude/settings.local.json`; Claude reads it from worktrees too.

## Safety
Worker panes get env vars that make `git push`, git over HTTPS and `gh` fail. That is an accident guard, not a sandbox: an agent that deliberately clears its environment can get around it. Read-only roles are enforced by Claude's permission mode and pi's tool list, and the orchestrator checks `git status` afterwards.

## Development
`python3 tests/test_nakama.py` (uses fake herdr/wt; no herdr needed).
