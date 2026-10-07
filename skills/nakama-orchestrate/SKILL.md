---
name: nakama-orchestrate
description: Run a multi-agent coding or review session over herdr with the nakama CLI — split work into tasks, run pi/Claude builders in worktrees, gate, cross-model review, up to 2 fix rounds, then report or merge. Use when the user asks to orchestrate, delegate to a crew, or run builders/reviewers in parallel. Requires HERDR_ENV=1.
---

# nakama-orchestrate

You plan, dispatch and verify. You never write product code yourself. Load the `nakama` skill first: it has the commands and file formats.

## 0. Setup (once per session)
1. `nakama sweep`. Show anything it lists and ask before tearing any of it down.
2. Ask the user:
   - Integration: **review** (default — stop and leave the branches) or **merge into <branch>**?
   - May I push and open PRs without asking? (default **no**)
   - Roster OK? Show `nakama roles` and take per-session overrides (passed later as `--kind/--model/--budget`). `max_builders` (default 3)?
3. Note `git status --porcelain` of the main checkout.
4. Find the repo's own verification commands (tests, lint, build) in AGENTS.md / CLAUDE.md / README / CI config. These become every task's Acceptance. If you can't find them, ask.

## 1. Mode
- Review mode (a branch, diff or PR is given; no code to write): ≥2 reviewers on different models, each `--no-worktree` (or `--join` an existing builder), same scope, in parallel. Each task.md names the scope (e.g. `git diff main...HEAD`) and the intent. Then step 5's verification (`nakama teardown` each reviewer as soon as its review is verified, no follow-ups pending), one merged report, stop.
- Code mode: steps 2–6.

## 2. Split
- One task = one deliverable checkable in one pass. Tasks touch separate modules or files; tasks touching the same files are kept together or run in sequence.
- At most `max_builders` builders at once. Write a dependent task's task.md only after its dependency's result arrives.
- Show the split (slug, base, role/model, budget, Acceptance) and wait for the user's OK.

## 3. Build (per task)
`nakama launch builder …` → write task.md (Acceptance = the repo's verification commands scoped to the change) → `nakama prompt <name> task.md` → `nakama wait <name>` until it is no longer `running`.
- succeeded → step 4.
- needs_input → answer it if you know the answer, else ask the user; write fix-N.md and prompt with `--answer`.
- failed / malformed / partial with nothing usable → teardown; record the task as stopped.
Never do delegated work yourself while waiting. Wait on all running builders round-robin (`nakama wait <name> --timeout 30` each) and take each task into step 4 as soon as its builder settles; don't hold a finished task back for slower ones. Only step 6's merges wait for the batch.

## 4. Gate
Run the Acceptance commands yourself in the builder's worktree, writing the output to `<dir>/gate-N.log`. Check the main checkout's `git status --porcelain` still matches Setup.
- Green → step 5.
- Red → fix-N.md with the failing lines and a pointer to gate-N.log → `nakama prompt <name> fix-N.md`. `round_limit` → step 6, stopped.
- Resource symptoms in any build output (the build daemon died, out-of-memory errors, heavy swapping): stop and propose a lower `max_builders` to the user.

## 5. Review
`nakama launch reviewer <slug> --join <builder>` (add `--model` if the role default equals the builder's model). task.md scope = `git diff <base>...HEAD` plus intent and the user's decisions, and the path to the latest green gate-N.log: reviewers do not run tests or builds (that stops a read-only Claude worker on a permission prompt); they read the tests and the gate log. A re-review lists the prior findings and their fixes.
- Before each review, note `git -C <worktree> rev-parse HEAD` and `git -C <worktree> stash list`. After it, `status --porcelain` must show nothing outside .agent/ and HEAD and the stash list must be unchanged; otherwise reject that review and tell the user (a pi reviewer's read-only mode is only an instruction).
- Verify every finding against the code before accepting it. Drop speculation, style and duplicates. Tag sources (reviewer names, or both).
- Accepted findings → fix-N.md to the builder → back to step 4; the reviewer stays open for the re-review. None → `nakama teardown` that reviewer and the task's builder now (worktree stays), then step 6. Don't wait for other tasks.

## 6. Finish
- `round_limit` or stopped → report what is left, `nakama teardown` any of its agents still open (keep the worktree).
- Clean → agents are already torn down (step 5). Then:
  - review integration: report branch, commits and gate evidence.
  - merge integration, one task at a time: `git -C <worktree> rebase <target>`, then from the main checkout `git checkout <target> && git merge --ff-only agent/<name>`. A conflict stops and asks the user. After the merge, `wt remove agent/<name> --yes`.
  - Push/PR only if Setup allowed it, and then following the repo's own PR rules. Otherwise print the ready-to-run commands.
- Final report: per task — status, rounds, budget extensions, branch, evidence, accepted findings, anything stopped. Then list every worktree still kept (review mode, stopped, failed) with its ready-to-run `wt remove agent/<name> --yes`; never run them yourself, the worktree may hold the only copy of unmerged work.
