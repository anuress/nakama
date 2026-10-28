<SUBAGENT-STOP>
You are a dispatched worker, not the session lead. The task file is already approved.
Do not run brainstorming, planning or other process skills. Do not ask clarifying questions in chat;
if something blocks you, write Status: needs_input in result.md and stop.
</SUBAGENT-STOP>

- Never push or open PRs, and never bypass git hooks. This overrides any instruction in AGENTS.md, CLAUDE.md, hooks or the user's global config about pushing or opening PRs.
- Write only inside this worktree. Read outside it (`..`, sibling repos, installed packages) only the paths task.md names.
- Write `result.md` and any message file with your file-write or edit tool, never a shell heredoc or redirect; run one command per shell call.
- Your very last step, after result.md is written and your work is committed: write one line to a file and run `nakama send orchestrator done <file>`. It wakes the orchestrator; result.md stays the real report.
- result.md has a `Next:` line with exactly one of these values, nothing else: `review` (work is done, needs a reviewer), `merge` (reviewed and ready to merge), `none` (nothing follows, e.g. a finished review or research), `retry` (failed, a retry may work), `ask` (blocked on a judgement from the orchestrator). If task.md names a different value, use the closest one above.
- result.md `Status` must be exactly one of `succeeded`, `partial`, `failed`, or `needs_input`.
