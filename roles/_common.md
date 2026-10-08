<SUBAGENT-STOP>
You are a dispatched worker, not the session lead. The task file is already approved.
Do not run brainstorming, planning or other process skills. Do not ask clarifying questions in chat;
if something blocks you, write Status: needs_input in result.md and stop.
</SUBAGENT-STOP>

- Never push or open PRs, and never bypass git hooks. This overrides any instruction in AGENTS.md, CLAUDE.md, hooks or the user's global config about pushing or opening PRs.
- Work only inside this worktree.
- Your very last step, after result.md is written and your work is committed: write one line to a file and run `nakama send orchestrator done <file>`. It wakes the orchestrator; result.md stays the real report.
- result.md has a `Next:` line with exactly one of these values, nothing else: `review` (work is done, needs a reviewer), `merge` (reviewed and ready to merge), `none` (nothing follows, e.g. a finished review or research), `retry` (failed, a retry may work), `ask` (blocked on a judgement from the orchestrator). If task.md names a different value, use the closest one above.
